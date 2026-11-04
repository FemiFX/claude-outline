#!/usr/bin/env python3
"""PostToolUse hook for Outline's MCP `create_collection`.

Outline's MCP server creates collections private and has no parameter to change that. This hook
sets the new collection's workspace permission through the REST API (`collections.update`)
right after it is created, so it does not depend on the agent remembering to, and tells the
agent the outcome.

Permission comes from OUTLINE_NEW_COLLECTION_PERMISSION: `read_write` (default), `read`, or
`private` to leave Outline's default alone. Needs OUTLINE_BASE_URL and OUTLINE_API_KEY.
Standard library only; never prints the key. Always exits 0 — a failure here must not undo or
block the collection that was already created.
"""

import json
import os
import sys
import urllib.error
import urllib.request

SKILL_HINT = (
    "Before adding pages, load the `outline` skill (claude-outline plugin) if it is not "
    "loaded yet and follow its page-structure rules: overview page first, one child page "
    "per topic, title as a field (no leading H1), sources at the end of each page."
)


def emit(message):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{message} {SKILL_HINT}",
        }
    }))
    sys.exit(0)


def collection_from(response):
    """Find the created collection's {id, url} in the MCP tool response."""
    blocks = response if isinstance(response, list) else [response]
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else block
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("id"):
            return data
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)

    created = collection_from(event.get("tool_response"))
    if not created:
        sys.exit(0)  # creation failed or an unexpected shape: nothing to fix up

    url = created.get("url", "")
    permission = os.environ.get("OUTLINE_NEW_COLLECTION_PERMISSION", "read_write").strip()
    if permission == "private":
        emit(f"The new collection {url} was left private (OUTLINE_NEW_COLLECTION_PERMISSION=private).")
    if permission not in ("read_write", "read"):
        emit(f"The new collection {url} is still PRIVATE: OUTLINE_NEW_COLLECTION_PERMISSION has "
             f"an unknown value {permission!r} (use read_write, read or private). Tell the user.")

    base = os.environ.get("OUTLINE_BASE_URL", "").rstrip("/")
    key = os.environ.get("OUTLINE_API_KEY", "")
    if not base or not key:
        emit(f"The new collection {url} is still PRIVATE: OUTLINE_BASE_URL / OUTLINE_API_KEY "
             "are not set for this hook. Tell the user so they can share it in Outline.")

    request = urllib.request.Request(
        f"{base}/api/collections.update",
        data=json.dumps({"id": created["id"], "permission": permission}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    error = None
    for _ in range(3):  # some networks drop the odd connection; retry those only
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                body = json.loads(reply.read())
            if body.get("ok") and body.get("data", {}).get("permission") == permission:
                emit(f"The new collection {url} was made visible to the whole workspace "
                     f"(permission {permission}).")
            error = f"unexpected reply: {str(body)[:200]}"
            break
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            error = str(exc)
    emit(f"The new collection {url} is still PRIVATE: setting its permission failed ({error}). "
         "Tell the user so they can share it in Outline.")


if __name__ == "__main__":
    main()
