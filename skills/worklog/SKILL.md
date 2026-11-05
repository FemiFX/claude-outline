---
name: worklog
description: Write or update this session's entry in the Outline session log (claude-outline's optional work map). Use when the user says "log this session", "log it", "/worklog", or asks to record what this session did, agreed and where it stopped. Only works where the session log is configured.
---

# Session log

The session log keeps one Outline entry per Claude session: what the user set out to do, what they asked, how Claude responded, what was agreed and why, what got done, where it stopped, and where it sits against the project's milestones. In Claude Code it is written automatically when a session ends. This skill writes it now, which helps when a session stays open for days.

## Claude Code (shell available)

Run the plugin's script for the current folder. The script sits two folders up from this skill, in `hooks/worklog.py`:

```bash
python3 "<plugin root>/hooks/worklog.py" now --cwd "$PWD"
```

- It prints `created: <url>` or `updated: <url>`. Give the user that link.
- `--dry-run` prints the entry without writing it.
- It takes about half a minute because a smaller model writes the summary.
- If it says the session log is off, tell the user which settings are missing. They are listed in the plugin README under "Session log". Don't write the entry by hand instead.

## No shell (Claude Desktop, claude.ai, mobile)

Follow the workspace guidance Outline sends with its MCP connection. If it names a work map collection:

1. `fetch` the Focus and Projects pages and the "Session entry template" in that collection.
2. Pick the project this conversation served, or Unassigned. Never invent a project.
3. Fill every heading of the template. Record what happened and what was agreed, with reasons. Don't add advice. Write "not decided" where nothing was agreed.
4. Put the entry under Session log, then the month page `YYYY-MM`. Create the month page if it's missing. Title it `YYYY-MM-DD · short title`.
5. If an entry for this conversation already exists, update it with `update_document` instead of creating a new one.
6. Never edit Focus, Projects or project pages, and never delete anything.
