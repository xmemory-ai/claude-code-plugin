# xmemory agent plugin

Persistent, schema-structured memory for coding agents. This plugin ships the shared **skills**
and **session hooks** that teach an agent when and how to use xmemory — see
[What ships](#what-ships). The memory itself is reached over the **xmemory remote MCP server**,
through an MCP entry registered **per instance** — see
[Connecting an instance](#connecting-an-instance). The plugin bundles no MCP entry of its own, so
`/mcp` never shows a connection you did not ask for.

> **First-party positioning.** xmemory is a first-party memory store: it holds the data you
> explicitly save to your xmemory instance, in xmemory's own backend. It does **not** read
> Claude's or Codex's built-in memory, your past chat history, or your files, email, or cloud
> drives — it only stores and returns what is written to this instance.

## Where this applies

The same repository is a **Claude Code** plugin and a **Codex** plugin. Claude Code reads
`.claude-plugin/plugin.json`; Codex and ChatGPT Work read `.codex-plugin/plugin.json`. Both
manifests reuse the same skills. Both clients also discover `hooks/hooks.json`; Codex uses that
conventional path automatically, so its manifest needs no `hooks` entry.

Claude Desktop, claude.ai, and mobile do not install this plugin. There, add xmemory manually as
a custom connector: Settings → Connectors → Add custom connector →
`https://mcp.xmemory.ai/instance/<id>`, where `<id>` is the instance id. All surfaces reach the
same remote MCP server.

## Connecting an instance

An MCP entry holds **one** connection to **one** instance, so each instance you use gets its own
named entry. Name it `xmemory-<id8>` — `xmemory-` followed by the first eight characters of the
instance id. That is the name the instance's own setup instructions print
(`xmemcli instance setup <id>`, or the Connect tab in the console), so following either produces
one entry rather than two under different names. Below, `<id>` is the instance id and `<id8>`
its first eight characters.

If the xmemory CLI is installed and signed in, prefer the **client form**: the agent client
starts `xmemcli mcp`, which reads the credential from the CLI's own configuration on every
connection, so nothing secret or session-specific is captured in the entry and no browser
sign-in is needed — now or in any later session.

```bash
# Claude Code
claude mcp add xmemory-<id8> -- xmemcli mcp <id>

# Codex
codex mcp add xmemory-<id8> -- xmemcli mcp <id>
```

Check `xmemcli --json status` first and read both `version` and `authenticated` rather than its
exit code. The `mcp` command requires at least `0.0.7`; upgrade an older client with
`uv tool install --upgrade xmemcli`. If credentials are absent, run `xmemcli auth login`
(browser, or `--email <address>` for a headless emailed approval). If the CLI is absent or cannot
be upgraded, use the **direct form** instead, which signs in through the browser once per entry:

```bash
# Claude Code — then authorize it with /mcp, or `claude mcp login xmemory-<id8>`
claude mcp add --transport http xmemory-<id8> "https://mcp.xmemory.ai/instance/<id>"

# Codex
codex mcp add xmemory-<id8> --url "https://mcp.xmemory.ai/instance/<id>"
codex mcp login xmemory-<id8>
```

Registering an entry is not the same as connecting it: an entry added mid-session usually
connects only after the client restarts. Every entry stays bound to its own instance, and any
number of them can be live at once. The bundled `connect` skill detects the active client and
shows only the applicable command; it also records which instances matter in this directory —
see [Project bindings](#project-bindings-xmemoryjson).

### Instance management

Creating, listing and reshaping instances is the **control plane**, and the CLI covers it:
`xmemcli instance create`, `xmemcli org list instances`, `xmemcli instance setup`,
`xmemcli xmd generate` / `xmemcli xmd enhance`, and `xmemcli schema …`. The plugin registers no
admin entry.

If you also want those operations as MCP tools, add the admin server yourself. It is a separate,
deliberate sign-in because it includes destructive operations such as deleting an instance:

```bash
# Claude Code
claude mcp add --transport http xmemory-admin https://mcp.xmemory.ai/admin

# Codex
codex mcp add xmemory-admin --url https://mcp.xmemory.ai/admin
```

Direct entries authorize via **OAuth 2.1 + PKCE** — a browser opens on first use; no static
tokens are pasted into the client. Connection walkthrough: **https://xmemory.ai/mcp**.

## Project bindings (`.xmemory.json`)

A **binding** records which instances an agent working in a given directory should know about,
and how eagerly to engage each one — `autoload` (pull its context every session), `available`
(engage on demand), or `off` (dormant). Ask the agent to "connect xmemory to this project" and the
bundled `connect` skill will discover your instances and write the file.

`.xmemory.json` holds **no secrets** — an instance id is not a credential — so the project-scope
file at the project root — the session root, which in a checkout is the work-tree root — is
meant to be committed and shared with the team. Two people
editing one binding at the same moment is last-writer-wins. Writes through `xmemcli` are atomic,
so the file is never left half-written, though a simultaneous edit can still be lost; if you edit
the file by hand, write to a temporary file and rename it to get the same guarantee. A private
`~/.xmemory.json` adds your personal instances; the two are merged like git config, with the
project file winning field by field — except that a `tier: off` you set for yourself is
reapplied afterwards, so a project binding cannot raise an instance you silenced.

When the directory being loaded *is* your home directory — a container that clones into `$HOME`,
or a session opened in `~` itself — your files and the project's are the same files, so there is
no separate user scope and session-start context asks you to set `XMEM_API_KEY` rather than guess
which credential is yours. Working in a project folder is unaffected, including on a machine whose
home directory is under version control.

Binding is not the same as connecting: the binding is local bookkeeping, while an MCP server entry
is what actually reads and writes an instance's data (see
[Connecting an instance](#connecting-an-instance) above).

Autoload additionally needs the [`xmemcli`](https://pypi.org/project/xmemcli/) command-line client
(`uv tool install xmemcli`), because pulling context at session start happens in a separate process
that cannot reach the MCP OAuth token and needs its own credential — acquired once with
`xmemcli auth login` (browser handoff), or headlessly with `xmemcli auth login --email <address>`,
where the single human action is approving a sign-in email. Everything else — binding, and
the instance context that arrives with the MCP connection — works without it.

## What ships

| Component | What it does |
|-----------|--------------|
| **`connect`** skill | Discovers your instances and writes the `.xmemory.json` binding |
| **`doctor`** skill | Reports which parts of the setup work, and what to do about the rest |
| **`xmemory-memory`** skill | Describes when to reach for the memory tools |
| **SessionStart** hook | Injects the context of instances bound `autoload` |
| **PreCompact** hook | Reminds the agent to persist durable facts before context is summarized away |
| Codex `AGENTS.md` manager | Adds, checks, or removes the marked global fallback block |

No MCP entry is bundled. Connections are registered per instance — by you, by
`xmemcli instance setup`, or by the `connect` skill — so an entry exists only for an instance you
chose, and it is authorized the way you registered it.

Both hooks are POSIX `sh` with **no dependencies** — no Node, Python or `jq`. The agent clients
are distributed as native binaries, so none of those interpreters is guaranteed to be present
alongside them. Codex exposes `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` compatibility
variables, so the same hook commands run from both manifests without a fork.

Neither hook can fail a session. A project with no `.xmemory.json` gets silence. A missing CLI,
an expired credential or an unreachable API costs context rather than blocking anything — and
says so in one line, because a session that receives nothing cannot tell "no memories" from
"could not reach them".

Already wired xmemory into Claude Code by hand? Installing the plugin does not replace your
hooks — both run, and context is injected twice. See [`MIGRATION.md`](MIGRATION.md);
`/xmemory:doctor`
reports the overlap. To keep your own hooks and stand these down, set `XMEMORY_DISABLE_HOOKS=1`;
the skills and your MCP entries are unaffected.

The PreCompact hook is a reminder, not an automatic upload. A hook has no model, so it cannot read
a session and decide what mattered, and no mechanical rule turns "this session" into rows of an
arbitrary schema — a grocery list and a CRM share nothing. The agent decides what to persist
and writes it through the memory tools it already has. Nothing is sent anywhere by the hook
itself.

## Install in Claude Code

This plugin is published as its own marketplace repo, so you can install it straight from GitHub:

```
/plugin marketplace add xmemory-ai/claude-code-plugin
/plugin install xmemory@xmemory-ai
```

Once approved, it's also available from Anthropic's community marketplace:

```
/plugin marketplace add anthropics/claude-plugins-community
/plugin install xmemory@claude-community
```

Restart Claude Code or run `/reload-plugins`, then [connect an instance](#connecting-an-instance):
`xmemcli instance setup <id>` prints the exact commands for this machine, and `/xmemory:connect`
records which instances this project uses.

(For local development: clone `xmemory-ai/claude-code-plugin`, run `claude --plugin-dir .` from
its root, and run `sh hooks/test_hooks.sh` and `sh test_manifest.sh` before publishing.)

## Install in Codex

Add this repository as a marketplace, install the plugin, and start a new session:

```text
codex plugin marketplace add xmemory-ai/claude-code-plugin
codex plugin add xmemory@xmemory-ai
```

Then [connect an instance](#connecting-an-instance) with the Codex commands shown there.

Codex lifecycle hooks must be enabled with `[features] hooks = true`. Plugin hooks are
non-managed hooks, so Codex skips them until the user reviews and trusts their current definition
with `/hooks`. The global `AGENTS.md` fallback and the `doctor` skill cover setup when hooks are
off or not yet trusted. Ask Codex to "run xmemory doctor" to check both states and offer the
reversible fallback installer.

For local development, add the repository root as a local marketplace:

```text
codex plugin marketplace add /path/to/claude-code-plugin
codex plugin add xmemory@xmemory-ai
```

The `.codex-plugin/plugin.json`, `skills/`, and `hooks/` live in the same plugin root as their
Claude Code counterparts. See [`CODEX.md`](CODEX.md) for the verified behavior, remaining
desktop checks, and cloud limitation.

## Tools

### Memory tools (a per-instance entry)

These are the tools an `xmemory-<id8>` entry exposes. With the direct form, the connect screen
lets you choose exactly which to authorize, and the **schema-management** group starts
unchecked; the client form grants the default groups.

**Core memory** (granted by default)

| Tool | What it does |
|---|---|
| `write_async` | Preferred write path — save create/update/delete intent in natural language; returns immediately. |
| `write` | Synchronous write; use only when you must read the same data back in the same turn. |
| `read` | Query the instance in natural language (lookups, aggregations, listings, traversals). |
| `write_status` | Diagnostic only — check once whether a specific async write landed, and how long it took. `queued`, `processing`, `extracting`, `extracted` and `applying` all mean it is still in flight. |
| `get_instance_id` | Return the instance ID for the current session. |
| `get_instance_schema` | Return the instance's object/field/relation schema. |

**Schema evolution from real traffic** (granted by default)

| Tool | What it does |
|---|---|
| `review_suggestions` | See schema improvements proposed from your read/write traffic. |
| `decide_suggestions` | Accept / reject / defer those proposals (bulk). |
| `apply_pending_decisions` | Commit accepted proposals as a migration. |

**Schema management** (opt-in — unchecked by default)

| Tool | What it does |
|---|---|
| `update_instance_schema` | Replace the schema (applied as a migration). |
| `dry_run_schema_migration` | Preview what `update_instance_schema` would do. |
| `list_schema_migrations` | Migration history, newest first. |
| `get_schema_migration` | One migration's detail. |
| `enhance_schema` | LLM improves a YAML schema from a description (returns YAML, does not apply). |

### Admin tools (the optional `xmemory-admin` entry)

Global account/fleet surface — no instance binding. Available once you add the admin entry
yourself (see [Instance management](#instance-management)); the CLI covers the same operations
without it.

| Tool | What it does |
|---|---|
| `admin_create_instance` | Create a new instance. |
| `admin_list_instances`, `admin_list_own_instances` | List instances. |
| `admin_get_instance`, `admin_get_instance_by_id` | Inspect an instance. |
| `admin_get_instance_schema_by_id` | Read an instance's schema (observability). |
| `admin_update_instance_metadata(_by_id)`, `admin_patch_instance_metadata(_by_id)` | Update name/description. |
| `admin_delete_instance`, `admin_delete_instance_by_id` | **Destructive** — permanently delete an instance. |
| `admin_list_clusters`, `admin_get_cluster` | Cluster observability. |
| `admin_generate_schema` | LLM: generate a fresh schema from a description. |
| `admin_enhance_schema` | LLM: improve an existing schema. |

## Support & legal

- Docs: https://xmemory.ai/mcp
- Privacy: https://xmemory.ai/privacy-policy.html
- Terms: https://xmemory.ai/terms-and-conditions.html

© xmemory Inc. All rights reserved. The contents of this plugin directory are proprietary.
Permission is granted to redistribute the plugin solely through the Claude Code and Codex plugin
directories and equivalent Anthropic and OpenAI distribution channels, and to install it solely
to connect to the xmemory service; all other rights are reserved. See [`LICENSE`](./LICENSE). Use
of xmemory is governed by the
[Terms & Conditions](https://xmemory.ai/terms-and-conditions.html).
