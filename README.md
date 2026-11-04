# claude-outline

Gives Claude agents read and write access to an [Outline](https://www.getoutline.com) wiki:
create collections, publish documents and nested page trees, search, read, edit and delete.

It builds on Outline's **built-in MCP server** and adds three things on top:

| Part | What it does |
|---|---|
| `.mcp.json` | Connects Claude Code to `$OUTLINE_BASE_URL/mcp` with `$OUTLINE_API_KEY`. |
| `skills/outline/` | Conventions for agents: check the connection, avoid duplicate collections, overview page + child pages, sources, patch-style edits, deletes. |
| `hooks/` | **New collections are made workspace-visible** (Outline's MCP creates them private and can't change that). **Every delete needs a person's confirmation**; runs that can't ask — headless, most subagents — are refused. |

## Requirements

- Outline **v1.6.0 or later** (tested on v1.12.0, self-hosted). For file uploads on the
  `local` storage driver, v1.9.2 or later.
- MCP switched on for the workspace: **Settings → Workspace → AI**. It is off by default for
  workspaces created before v1.6.0.
- An API key: **Settings → API & Apps**, with read and write access. Agents act as the key's
  user — everything they create is authored by that account.
- Claude Code (tested on 2.1.206). The visibility hook needs `python3` on the machine.

## Install — Claude Code (CLI, IDE, headless servers, subagents)

1. Set the two variables where Claude Code will see them, for example in `~/.zshrc` /
   `~/.bashrc`, or the `env` block of `~/.claude/settings.json`:

   ```bash
   export OUTLINE_BASE_URL=https://outline.example.com
   export OUTLINE_API_KEY=ol_api_...
   ```

2. Install the plugin — either way works:

   ```bash
   # a) clone into your skills directory; loads automatically from the next session
   git clone https://github.com/FemiFX/claude-outline ~/.claude/skills/claude-outline

   # b) or as a plugin marketplace
   claude plugin marketplace add FemiFX/claude-outline
   claude plugin install claude-outline@claude-outline
   ```

3. Start a new session and ask: *"Which collections are in Outline?"*

Update with `git pull` in the clone, or `claude plugin marketplace update claude-outline`.

### Options

| Variable | Default | Meaning |
|---|---|---|
| `OUTLINE_NEW_COLLECTION_PERMISSION` | `read_write` | What workspace members may do in collections agents create: `read_write`, `read`, or `private` (leave Outline's default). |

### Headless and multi-agent runs

`claude -p` runs need the Outline tools allowed, e.g.
`--allowedTools mcp__plugin_claude-outline_outline`. Deletes are refused in these runs by
design; do them interactively. When an agent fans work out to subagents, have the orchestrator
create the collection and an overview page first and pass their ids on — the skill describes
this.

## Install — Claude Desktop and claude.ai

1. Add a connector with the URL `https://<your-outline>/mcp` and sign in with your Outline
   account when asked.
2. Optional: upload `skills/outline/` as a skill (zip the folder).

These clients have no shell, so the hooks don't run there: collections created from them stay
private, and deletes are guarded only by the skill's instructions (and by any per-tool approval
setting your client offers). The skill therefore has these clients publish into an **existing
shared collection**. Name it for every client at once in Outline's **Settings → Workspace → AI →
additional guidance**, e.g. *"Agents without a shell publish research into the collection
'Research'."* — Outline passes that text to every MCP client.

## Security

- The API key has the full rights of its user. Use a dedicated Outline user if you want agent
  activity separated and revocable on its own.
- The key is only read from the environment; nothing in this repo stores it. Don't commit it.

## License

[MIT](LICENSE)
