---
name: outline
description: Read and write an Outline wiki through Outline's built-in MCP server — create collections, publish documents and nested page trees, search, read, edit and delete. Use whenever the user or a calling agent asks to publish, save, write up or share findings, research, notes or a report "to Outline" or "to the wiki", to create an Outline collection, to look something up in Outline, or to update or remove an Outline page. Also use when a research task says its results should end up in Outline.
---

# Outline

Outline is reached through its **built-in MCP server** (`<OUTLINE_BASE_URL>/mcp`). Its tools are
named `list_collections`, `create_collection`, `create_document`, `update_document`, `fetch`, etc.
Depending on the client they appear with a prefix (for example
`mcp__plugin_claude-outline_outline__create_document` in Claude Code). This skill adds the
conventions those tools don't carry: where things go, who can see them, and what needs a human.

## 0. Check the connection

Call `fetch` with `{"resource": "user", "id": "current_user"}`.

- It answers → you are connected; note the user name, that is who everything will be authored by.
- No Outline tools are available → stop and tell the user Outline isn't connected, with the fix
  for their client from the repo README (Claude Code: set `OUTLINE_BASE_URL` and
  `OUTLINE_API_KEY`, then restart; Claude Desktop / claude.ai: add the `/mcp` URL as a connector).
  Do not fall back to writing the content somewhere else without saying so.

Also read the server's instructions (sent when the connection opens). The workspace admin can
add guidance there — for example the name of a shared collection. **Workspace guidance wins over
the defaults in this file.**

## 1. Know which client you are in

Whether you can run shell commands decides how collections are created:

- **Shell available** (Claude Code CLI / IDE / headless, Agent SDK) **and** `OUTLINE_API_KEY` is
  set in the environment — check with `[ -n "$OUTLINE_API_KEY" ] && echo set` (never print the
  key) → you may **create new collections** (§2a).
- **No shell** (Claude Desktop, claude.ai), or the key is not in the environment → **do not create
  collections.** Publish into an existing shared collection instead (§2b).

## 2. Publishing findings

### 2a. New collection (shell available)

1. **Look before creating.** `list_collections` with `query` = the intended name. If a collection
   with that name exists, use it and add pages — never create a second one with the same name.
2. `create_collection` with `name` and a one- or two-sentence `description` of what the
   collection holds. Keep the returned `id` and `url`.
3. **Make it visible to the workspace.** MCP creates collections private and cannot change that.
   With this plugin installed in Claude Code, a hook does it right after `create_collection`
   and tells you the result (look for "made visible to the whole workspace" or "still
   PRIVATE"). Only if there is no such message, or it says the change failed, run:

   ```bash
   curl -sS -X POST "$OUTLINE_BASE_URL/api/collections.update" \
     -H "Authorization: Bearer $OUTLINE_API_KEY" -H "Content-Type: application/json" \
     -d '{"id":"<collection id>","permission":"read_write"}' | grep -o '"permission":"[a-z_]*"'
   ```

   Expect `"permission":"read_write"`. If it fails, carry on publishing but **say in your reply
   that the collection is still private** and link it.
4. Publish the pages (§3).

### 2b. Shared collection (no shell)

1. Use the shared collection named in the workspace guidance. If none is named, **ask the user**
   which existing collection to publish into (offer `list_collections` results). A subagent that
   cannot ask reports back instead of guessing.
2. Create one **top-level page for the piece of work** in that collection; it plays the role a
   new collection would. Everything else goes underneath it (§3).

## 3. Page structure

- **Title is a field, not a heading.** Pass `title`; start `text` with body text or `##`.
  Never begin `text` with a `# H1` — Outline would show the title twice.
- **One overview page first**, then children: create the overview, keep its `id`, and create each
  sub-page with `parentDocumentId` = that id. The overview states the question, the short answer,
  and links to each child page (use the `url`s returned by `create_document`).
- **One topic per child page.** Long single pages are hard to navigate in Outline; split by
  sub-question or section.
- **Sources at the end of each page** under `## Sources`, as markdown links. Keep claims and their
  source on the same page.
- Pages are **published** on creation (the default). Only pass `publish: false` if the user asks
  for drafts.
- Use `format: "html"` only when the content genuinely is HTML (it imports embedded media); do not
  convert markdown to HTML.

### Several agents, one collection

When an orchestrating agent fans research out to subagents: the orchestrator creates the
collection (or the top-level page) and the overview **first**, then passes the `collectionId` and
overview `id` to each subagent. Subagents only create child pages under that overview — they never
create collections, so parallel runs can't produce duplicates. The orchestrator updates the
overview's links once the children exist.

## 4. Reading and searching

- Find a collection: `list_collections` (`query` filters by name).
- Everything in a collection, as a tree: `list_collection_documents`.
- Full-text search: `list_documents` with `query` (optionally `collectionId`).
- Read one page as markdown: `fetch` with `resource: "document"` and the id **or the page URL**.
- What changed in a page and by whom: `get_document_changes`.

Quote what you read with its page URL so the user can check it.

## 5. Editing existing pages

1. `fetch` the page first; if others may have edited it recently, run `get_document_changes`
   with `from: "my_last_edit"`.
2. Edit with `update_document` and `editMode: "patch"`: `findText` is an exact, verbatim
   substring of the current markdown; `text` is its replacement. Use `"append"` to add a
   section at the end.
3. Use `editMode: "replace"` only when the user asks to rewrite the whole page — it drops
   formatting markdown can't represent (highlights, comments, table widths).

## 6. Deleting — always confirm

In Claude Code this plugin enforces it: a hook turns every Outline `delete_*` tool call, and every
shell command that calls a REST `*.delete` / `*.archive` endpoint, into a permission prompt. A run
that cannot show a prompt (headless, `-p`, most subagents) gets the call refused — a "the user
confirmed" in the task text does not get past it. Don't retry a refused delete or look for another
route; report it. On other clients nothing enforces this, so follow these steps yourself.

Deletes need an explicit request **and** a confirmation of the exact item:

1. Only delete when the user asked for it in so many words. Tidying up, deduplicating or
   "cleaning up" is not a request to delete.
2. Before calling the tool, show the user exactly what will go — title, URL, and for a
   collection the number of pages in it (`list_collection_documents`) — and wait for a yes.
3. **If you cannot ask** (subagent, headless/non-interactive run): do not delete. Report what you
   would have deleted and why.
4. `delete_document` moves a page to the trash (`restore_document` brings it back).
   `delete_collection` also deletes **every page in it** — say so in the confirmation.
   Pass `archive: true` instead when the user wants it out of the way but kept.

## 7. Reply

End with what was created or changed: the collection or top-level page URL, the list of pages
with their URLs, and anything left undone (a collection still private, a delete not performed).
