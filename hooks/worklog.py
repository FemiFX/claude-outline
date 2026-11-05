#!/usr/bin/env python3
"""Session log for Outline: one entry per Claude Code session, linked to a project.

Modes
  session-end   SessionEnd hook. Checks the guards, queues the job, starts a detached
                worker and returns at once, so closing Claude is never slowed down.
  flush         SessionStart hook. Starts a detached worker for anything still queued.
  worker        Processes the queue (internal).
  now           Logs the current session in the foreground and prints the entry URL.
                Used by the /worklog skill. Options: --cwd DIR, --session ID, --dry-run.

Off unless OUTLINE_WORKLOG_COLLECTION_ID is set (environment or config file).
Standard library only. Never prints the API key. Never deletes. Only creates and updates
its own entries; never edits the Focus, Projects or project pages.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = Path.home() / ".claude" / "outline-worklog"
QUEUE = HOME_DIR / "queue"
LOG = HOME_DIR / "worklog.log"
SESSIONS = HOME_DIR / "sessions.json"
CONFIG_FILE = HOME_DIR / "config.json"

KEYS = [
    "OUTLINE_BASE_URL", "OUTLINE_API_KEY",
    "OUTLINE_WORKLOG_COLLECTION_ID", "OUTLINE_WORKLOG_LOG_PARENT_ID",
    "OUTLINE_WORKLOG_PROJECTS_ID", "OUTLINE_WORKLOG_FOCUS_ID", "OUTLINE_WORKLOG_UNASSIGNED_ID",
    "OUTLINE_WORKLOG_MODEL", "OUTLINE_WORKLOG_MIN_TURNS", "OUTLINE_WORKLOG_EXCLUDE_DIRS",
]

MAX_TRANSCRIPT_CHARS = 120_000
MAX_USER_CHARS = 4_000
MAX_ASSISTANT_CHARS = 2_500
MAX_PAGE_CHARS = 4_000

SECRET_PATTERNS = [
    r"ol_api_[A-Za-z0-9]{10,}",
    r"sk-ant-[A-Za-z0-9_\-]{10,}",
    r"sk-[A-Za-z0-9]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[abpr]-[A-Za-z0-9\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b",  # IBAN-like
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+",
]


# ── small helpers ────────────────────────────────────────────────────────────

def log(msg):
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def redact(text):
    for pat in SECRET_PATTERNS:
        text = re.sub(pat, "[redacted]", text)
    return text


def login_shell_env(names):
    """Values a login shell would export. Covers editors that start Claude Code without
    reading ~/.zshrc or ~/.bashrc. Prints nothing; returns {} on any failure."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    script = "; ".join(f'printf "%s\\0" "${{{n}}}"' for n in names)
    try:
        out = subprocess.run([shell, "-lic", f"printf '__WL__'; {script}"], capture_output=True,
                             timeout=10, stdin=subprocess.DEVNULL).stdout.decode(errors="replace")
        values = out.split("__WL__", 1)[1].split("\0")
        return {n: v for n, v in zip(names, values) if v}
    except Exception:
        return {}


def load_config():
    """Environment first, then ~/.claude/outline-worklog/config.json, then a login shell."""
    cfg = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    try:
        for k, v in json.loads(CONFIG_FILE.read_text()).items():
            if k in KEYS and v and k not in cfg:
                cfg[k] = str(v)
    except Exception:
        pass
    missing = [k for k in KEYS if k not in cfg]
    # Only users who turned the feature on pay for the shell lookup.
    if missing and (cfg.get("OUTLINE_WORKLOG_COLLECTION_ID") or CONFIG_FILE.exists()):
        cfg.update(login_shell_env(missing))
    cfg.setdefault("OUTLINE_WORKLOG_MODEL", "claude-sonnet-5-5")
    cfg.setdefault("OUTLINE_WORKLOG_MIN_TURNS", "2")
    return cfg


def enabled(cfg):
    need = ["OUTLINE_BASE_URL", "OUTLINE_API_KEY", "OUTLINE_WORKLOG_COLLECTION_ID",
            "OUTLINE_WORKLOG_LOG_PARENT_ID"]
    return all(cfg.get(k) for k in need)


class Outline:
    def __init__(self, cfg):
        self.base = cfg["OUTLINE_BASE_URL"].rstrip("/")
        self.key = cfg["OUTLINE_API_KEY"]

    def __call__(self, method, payload):
        req = urllib.request.Request(
            f"{self.base}/api/{method}", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        if not data.get("ok", True):
            raise RuntimeError(f"{method}: {data.get('message')}")
        return data.get("data")

    def url(self, doc):
        return self.base + doc["url"]


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ── transcript ───────────────────────────────────────────────────────────────

TAG_BLOCKS = re.compile(
    r"<(system-reminder|ide_selection|ide_opened_file|local-command-stdout|local-command-caveat|"
    r"command-message|command-args)>[\s\S]*?</\1>")


def block_texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
    return out


def clip(text, n):
    text = text.strip()
    return text if len(text) <= n else text[:n] + " […]"


def condense(transcript_path):
    """Keep user prompts, Claude's text and tool names. Drop tool output."""
    lines, turns, first_ts, last_ts = [], 0, None, None
    ask_ids = set()  # AskUserQuestion calls: the answers carry decisions, so keep them
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("isSidechain") or e.get("isMeta"):
                continue
            ts = e.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            msg = e.get("message") or {}
            role = msg.get("role") or e.get("type")
            content = msg.get("content")
            if role == "user":
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in ask_ids:
                        answer = b.get("content")
                        answer = answer if isinstance(answer, str) else "\n".join(block_texts(answer))
                        lines.append(f"USER answered: {clip(answer, MAX_USER_CHARS)}")
                text = "\n".join(block_texts(content))
                if "<command-name>" in text:
                    name = re.search(r"<command-name>(.*?)</command-name>", text)
                    lines.append(f"USER ran command {name.group(1) if name else ''}")
                    continue
                text = TAG_BLOCKS.sub("", text).strip()
                if not text or text.startswith("<"):
                    continue
                turns += 1
                lines.append(f"USER: {clip(text, MAX_USER_CHARS)}")
            elif role == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        lines.append(f"CLAUDE: {clip(b['text'], MAX_ASSISTANT_CHARS)}")
                    elif b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                        ask_ids.add(b.get("id"))
                        qs = (b.get("input") or {}).get("questions") or []
                        for q in qs:
                            opts = " / ".join(o.get("label", "") for o in q.get("options", []))
                            lines.append(f"CLAUDE asked: {q.get('question')} [{opts}]")
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        hint = (inp.get("description") or inp.get("file_path") or inp.get("query")
                                or inp.get("url") or inp.get("skill") or "")
                        lines.append(f"  [tool {b.get('name')}: {clip(str(hint), 160)}]")
    text = redact("\n".join(lines))
    if len(text) > MAX_TRANSCRIPT_CHARS:
        head = MAX_TRANSCRIPT_CHARS // 4
        text = text[:head] + "\n\n[… middle of session omitted for length …]\n\n" + text[-(MAX_TRANSCRIPT_CHARS - head):]
    return text, turns, first_ts, last_ts


def transcript_for(cwd, session_id=None):
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    folder = Path.home() / ".claude" / "projects" / slug
    if session_id:
        p = folder / f"{session_id}.jsonl"
        return p if p.exists() else None
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def local_time(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        return datetime.now().astimezone()


# ── Outline context ──────────────────────────────────────────────────────────

SECTION = re.compile(r"^## (.+)$", re.M)
ENTRY_TITLE = re.compile(r"^\d{4}-\d{2}-\d{2} ")


def sections(text, wanted):
    parts = SECTION.split(text or "")
    found = {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return "\n".join(f"{k}: {found[k]}" for k in wanted if found.get(k))


def gather_context(api, cfg):
    def text_of(doc_id):
        if not doc_id:
            return ""
        try:
            return api("documents.info", {"id": doc_id}).get("text", "")
        except Exception as exc:
            log(f"could not read {doc_id}: {exc}")
            return ""

    focus = text_of(cfg.get("OUTLINE_WORKLOG_FOCUS_ID"))
    projects_page = text_of(cfg.get("OUTLINE_WORKLOG_PROJECTS_ID"))
    projects, unassigned = [], None
    pid = cfg.get("OUTLINE_WORKLOG_PROJECTS_ID")
    if pid:
        try:
            children = api("documents.list", {"parentDocumentId": pid, "limit": 100})
        except Exception:
            children = []
        for d in children:
            if d["id"] == cfg.get("OUTLINE_WORKLOG_UNASSIGNED_ID"):
                unassigned = d
                continue
            recent = []
            try:
                hits = api("documents.list", {"backlinkDocumentId": d["id"], "sort": "updatedAt",
                                              "direction": "DESC", "limit": 10})
                for b in [h for h in hits if ENTRY_TITLE.match(h.get("title", ""))][:2]:
                    recent.append(f"  - {b['title']}\n" + sections(
                        b.get("text", ""), ["Where we left off", "Where this sits in the grand scheme",
                                            "What's next, and why"]))
            except Exception:
                pass
            projects.append({"id": d["id"], "title": d["title"], "url": api.url(d),
                             "text": clip(d.get("text", ""), MAX_PAGE_CHARS), "recent": recent})
    if unassigned is None and cfg.get("OUTLINE_WORKLOG_UNASSIGNED_ID"):
        try:
            unassigned = api("documents.info", {"id": cfg["OUTLINE_WORKLOG_UNASSIGNED_ID"]})
        except Exception:
            unassigned = None
    return focus, projects_page, projects, unassigned


# ── summarise ────────────────────────────────────────────────────────────────

def read_override(name):
    custom = HOME_DIR / name
    return custom.read_text() if custom.exists() else (PLUGIN_ROOT / "worklog" / name).read_text()


def claude_bin():
    for c in (shutil.which("claude"), str(Path.home() / ".claude" / "local" / "claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude", str(Path.home() / ".local" / "bin" / "claude")):
        if c and Path(c).exists():
            return c
    raise RuntimeError("claude CLI not found")


def summarise(cfg, meta, transcript, focus, projects_page, projects, unassigned):
    prompt = read_override("summarizer-prompt.md").replace(
        "<<session-entry-template.md>>", read_override("session-entry-template.md"))
    proj_lines = []
    for p in projects:
        proj_lines.append(f"### {p['title']}\nid: {p['id']}\nurl: {p['url']}\n{p['text']}")
        if p["recent"]:
            proj_lines.append("Last log entries linked to it:\n" + "\n".join(p["recent"]))
    if unassigned:
        proj_lines.append(f"### Unassigned\nid: UNASSIGNED\nurl: {cfg['OUTLINE_BASE_URL'].rstrip('/')}{unassigned['url']}")
    full = "\n\n".join([
        prompt,
        "META:\n" + json.dumps(meta, ensure_ascii=False, indent=2),
        "FOCUS:\n" + (focus or "(empty)"),
        "PROJECTS PAGE:\n" + (projects_page or "(empty)"),
        "PROJECTS:\n" + ("\n\n".join(proj_lines) or "(none yet)"),
        "TRANSCRIPT:\n" + transcript,
    ])
    run_dir = HOME_DIR / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OUTLINE_WORKLOG_CHILD="1")
    proc = subprocess.run(
        [claude_bin(), "-p", "--model", cfg["OUTLINE_WORKLOG_MODEL"], "--tools", "",
         "--no-session-persistence", "--strict-mcp-config", "--output-format", "json"],
        input=full.encode(), capture_output=True, timeout=600, cwd=run_dir, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr.decode()[:400]}")
    result = json.loads(proc.stdout.decode()).get("result", "")
    m = re.search(r"\{[\s\S]*\}", result)
    if not m:
        raise RuntimeError(f"summariser returned no JSON: {result[:300]}")
    out = json.loads(m.group(0))
    out["body"] = redact(out.get("body", ""))
    return out


# ── write to Outline ─────────────────────────────────────────────────────────

def month_page(api, cfg, month):
    parent = cfg["OUTLINE_WORKLOG_LOG_PARENT_ID"]
    for d in api("documents.list", {"parentDocumentId": parent, "limit": 100}):
        if d["title"] == month:
            return d
    d = api("documents.create", {"collectionId": cfg["OUTLINE_WORKLOG_COLLECTION_ID"],
                                 "parentDocumentId": parent, "title": month, "publish": True,
                                 "text": f"Session log entries for {month}, newest first."})
    try:
        api("documents.move", {"id": d["id"], "collectionId": cfg["OUTLINE_WORKLOG_COLLECTION_ID"],
                               "parentDocumentId": parent, "index": 0})
    except Exception:
        pass
    return d


def existing_entry(api, cfg, session_id):
    known = read_json(SESSIONS, {}).get(session_id)
    if known:
        try:
            d = api("documents.info", {"id": known})
            if not d.get("deletedAt") and not d.get("archivedAt"):
                return d
        except Exception:
            pass
    try:
        for hit in api("documents.search", {"query": f'"{session_id}"',
                                            "collectionId": cfg["OUTLINE_WORKLOG_COLLECTION_ID"], "limit": 5}):
            if session_id in (hit["document"].get("text") or ""):
                return hit["document"]
    except Exception:
        pass
    return None


def publish(api, cfg, job, entry, projects, unassigned):
    sid = job["session_id"]
    ids = {p["id"]: p for p in projects}
    project = ids.get(entry.get("project_id"))
    body = entry["body"]
    link = (project["url"] if project else
            (cfg["OUTLINE_BASE_URL"].rstrip("/") + unassigned["url"]) if unassigned else None)
    if link and link not in body:
        name = project["title"] if project else "Unassigned"
        body = f"**Project:** [{name}]({link})\n\n" + body
    if sid not in body:
        body += f"\n\n---\nSession id: {sid}"
    title = clip(entry.get("title") or f"{job['meta']['date']} · session", 100)

    found = existing_entry(api, cfg, sid)
    if found:
        api("documents.update", {"id": found["id"], "title": title, "text": body})
        doc = found
        action = "updated"
    else:
        month = month_page(api, cfg, job["meta"]["date"][:7])
        doc = api("documents.create", {"collectionId": cfg["OUTLINE_WORKLOG_COLLECTION_ID"],
                                       "parentDocumentId": month["id"], "title": title,
                                       "text": body, "publish": True})
        try:
            api("documents.move", {"id": doc["id"], "collectionId": cfg["OUTLINE_WORKLOG_COLLECTION_ID"],
                                   "parentDocumentId": month["id"], "index": 0})
        except Exception:
            pass
        action = "created"
    sessions = read_json(SESSIONS, {})
    sessions[sid] = doc["id"]
    write_json(SESSIONS, sessions)
    return action, api.url(doc)


# ── jobs ─────────────────────────────────────────────────────────────────────

def build_job(event):
    tp = event.get("transcript_path")
    if not tp or not Path(tp).exists():
        return None, "no transcript"
    cfg = load_config()
    transcript, turns, first_ts, last_ts = condense(tp)
    if turns < int(cfg.get("OUTLINE_WORKLOG_MIN_TURNS", "2")):
        return None, f"only {turns} user turn(s)"
    start, end = local_time(first_ts or ""), local_time(last_ts or "")
    meta = {
        "date": end.strftime("%Y-%m-%d"),  # entries are filed by the day the work stopped
        "started": start.strftime("%Y-%m-%d %H:%M"), "ended": end.strftime("%Y-%m-%d %H:%M"),
        "device": socket.gethostname().split(".")[0],
        "client": {"cli": "Claude Code CLI", "claude-vscode": "Claude Code in VS Code",
                   "claude-desktop": "Claude Code in Desktop"}.get(
                       os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""), "Claude Code"),
        "folder": event.get("cwd", ""),
        "session_id": event.get("session_id", ""),
        "end_reason": event.get("reason", ""),
    }
    return {"session_id": meta["session_id"], "meta": meta, "transcript": transcript,
            "queued_at": time.time()}, None


def excluded(cfg, cwd):
    dirs = [d for d in cfg.get("OUTLINE_WORKLOG_EXCLUDE_DIRS", "").split(":") if d]
    cwd = os.path.realpath(cwd or "")
    return any(cwd == os.path.realpath(d) or cwd.startswith(os.path.realpath(d) + os.sep) for d in dirs)


def process(job, dry_run=False):
    cfg = load_config()
    api = Outline(cfg)
    focus, projects_page, projects, unassigned = gather_context(api, cfg)
    entry = summarise(cfg, job["meta"], job["transcript"], focus, projects_page, projects, unassigned)
    if dry_run:
        return "dry-run", entry.get("title", "") + "\n\n" + entry.get("body", "")
    return publish(api, cfg, job, entry, projects, unassigned)


def spawn_worker():
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker"],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, env=dict(os.environ, OUTLINE_WORKLOG_CHILD="1"))


def run_queue():
    lock = HOME_DIR / "worker.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 1800:
            return
        lock.unlink(missing_ok=True)
        return run_queue()
    try:
        for path in sorted(QUEUE.glob("*.json"), key=lambda p: p.stat().st_mtime):
            item = read_json(path, None)
            if not item:
                path.unlink(missing_ok=True)
                continue
            if "meta" in item:
                job = item
            else:
                job, why = build_job(item)
                if not job:
                    log(f"skipped {item.get('session_id')}: {why}")
                    path.unlink(missing_ok=True)
                    continue
                job["attempts"] = item.get("attempts", 0)
            try:
                action, url = process(job)
                log(f"{action} {url} for session {job['session_id']}")
                path.unlink(missing_ok=True)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                log(f"Outline unreachable, kept in queue: {job['session_id']}: {exc}")
                break
            except Exception as exc:
                job["attempts"] = job.get("attempts", 0) + 1
                log(f"failed ({job['attempts']}) {job['session_id']}: {exc}")
                if job["attempts"] >= 3:
                    path.rename(path.with_suffix(".failed"))
                else:
                    write_json(path, job)
    finally:
        lock.unlink(missing_ok=True)


# ── entry points ─────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode in ("session-end", "flush") and os.environ.get("OUTLINE_WORKLOG_CHILD"):
        return  # the summariser's own session, or a worker: never log it

    if mode == "session-end":
        try:
            event = json.load(sys.stdin)
        except ValueError:
            return
        cfg = load_config()
        if not enabled(cfg) or excluded(cfg, event.get("cwd")):
            return
        if not event.get("session_id"):
            return
        event["queued_at"] = time.time()
        write_json(QUEUE / f"{event['session_id']}.json", event)
        spawn_worker()

    elif mode == "flush":
        if any(QUEUE.glob("*.json")) and enabled(load_config()):
            spawn_worker()

    elif mode == "worker":
        run_queue()

    elif mode == "now":
        args = sys.argv[2:]
        opt = lambda name: args[args.index(name) + 1] if name in args else None
        cwd = opt("--cwd") or os.getcwd()
        cfg = load_config()
        if not enabled(cfg):
            sys.exit("Session log is off: set OUTLINE_BASE_URL, OUTLINE_API_KEY, "
                     "OUTLINE_WORKLOG_COLLECTION_ID and OUTLINE_WORKLOG_LOG_PARENT_ID.")
        tp = transcript_for(cwd, opt("--session"))
        if not tp:
            sys.exit(f"No transcript found for {cwd}")
        job, why = build_job({"transcript_path": str(tp), "cwd": cwd,
                              "session_id": tp.stem, "reason": "manual"})
        if not job:
            sys.exit(f"Not logged: {why}")
        action, result = process(job, dry_run="--dry-run" in args)
        print(result if action == "dry-run" else f"{action}: {result}")

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # a hook must never break the session
        log(f"error in {sys.argv[1:]}: {exc}")
        if len(sys.argv) > 1 and sys.argv[1] == "now":
            raise
