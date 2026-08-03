# xmemory — Claude Code plugin

Persistent, schema-structured memory for Claude. This plugin registers the **xmemory remote
MCP servers** so Claude Code can save and recall your own data on demand, and ships the skills and
session hooks that tell Claude when to use them — see [What ships](#what-ships).

> **First-party positioning.** xmemory is a first-party memory store: it holds the data you
> explicitly save to your xmemory instance, in xmemory's own backend. It does **not** read
> Claude's built-in memory, your past chat history, or your files, email, or cloud drives — it
> only stores and returns what is written to this instance.

## Where this applies

This is a **Claude Code** plugin (terminal CLI + IDE extensions), installed via `/plugin` and
the plugin marketplace. **Claude Desktop, claude.ai, and mobile** do not install plugins — there,
add xmemory manually as a custom connector: Settings → Connectors → Add custom connector →
`https://mcp.xmemory.ai`. Both surfaces reach the same remote MCP server.

## Two connections

The plugin registers two MCP servers; connect and authorize them **separately** depending on
what you need:

- **`xmemory`** — your **memory / data plane**. Connect to a single instance and read/write its
  data. On sign-in you choose the instance to bind. This is what the bundled skill drives.
- **`xmemory-admin`** — your **instance-management / control plane**. Create, list, and manage
  your instances and their schemas. This connection includes powerful and destructive
  operations (e.g. deleting an instance), so it is a deliberate, separate sign-in.

Each connection authorizes independently via **OAuth 2.1 + PKCE** — a browser opens on first
use; no static tokens are pasted into the client. Connection walkthrough:
**https://xmemory.ai/mcp**.

### One instance vs. several

The bundled `xmemory` server points at the root URL `https://mcp.xmemory.ai`. A single server
entry holds **one** connection, so it is bound to **one** instance at a time — you pick that
instance **interactively in the OAuth sign-in screen** (it has an instance field), and you
re-authorize the same entry to switch to a different instance. The published manifest cannot
pre-fill an instance ID, because it is shared by every user.

To connect to **multiple instances at the same time**, add one named server **per instance** in
your own `.mcp.json`, each using the `/instance/<ID>` deep-link so the URLs (and therefore the
connections) are distinct:

```json
{
  "mcpServers": {
    "xmemory-work":     { "type": "http", "url": "https://mcp.xmemory.ai/instance/<work-instance-id>" },
    "xmemory-personal": { "type": "http", "url": "https://mcp.xmemory.ai/instance/<personal-instance-id>" }
  }
}
```

Each entry authorizes separately and stays bound to its own instance, so they are live
concurrently. This is a per-user customization and is intentionally not part of the shared
plugin manifest.

## Project bindings (`.xmemory.json`)

A **binding** records which instances an agent working in a given directory should know about,
and how eagerly to engage each one — `autoload` (pull its context every session), `available`
(engage on demand), or `off` (dormant). Ask Claude to "connect xmemory to this project" and the
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
is what actually reads and writes an instance's data (see [One instance vs.
several](#one-instance-vs-several) above).

Autoload additionally needs the [`xmemcli`](https://pypi.org/project/xmemcli/) command-line client
(`uv tool install xmemcli`), because pulling context at session start happens in a separate process
that cannot reach the MCP OAuth token and needs its own credential. Everything else — binding, and
the instance context that arrives with the MCP connection — works without it.

## What ships

| Component | What it does |
|-----------|--------------|
| `xmemory` MCP server | Read and write one instance's data |
| `xmemory-admin` MCP server | Create, list and manage instances and schemas |
| **`connect`** skill | Discovers your instances and writes the `.xmemory.json` binding |
| **`doctor`** skill | Reports which parts of the setup work, and what to do about the rest |
| **`xmemory-memory`** skill | Tells Claude when to reach for the memory tools |
| **SessionStart** hook | Injects the context of instances bound `autoload` |
| **PreCompact** hook | Reminds Claude to persist durable facts before context is summarized away |

Both hooks are POSIX `sh` with **no dependencies** — no Node, Python or `jq`. Claude Code is
distributed as a native binary, so none of those interpreters is guaranteed to be present
alongside it; a POSIX shell is, because Claude Code needs one for its own shell tooling on every
platform it supports.

Neither hook can fail a session. A project with no `.xmemory.json` gets silence. A missing CLI,
an expired credential or an unreachable API costs context rather than blocking anything — and
says so in one line, because a session that receives nothing cannot tell "no memories" from
"could not reach them".

Already wired xmemory into Claude Code by hand? Installing the plugin does not replace your
hooks — both run, and context is injected twice. See [`MIGRATION.md`](MIGRATION.md);
`/xmemory:doctor`
reports the overlap. To keep your own hooks and stand these down, set `XMEMORY_DISABLE_HOOKS=1`;
the skills and MCP servers are unaffected.

The PreCompact hook is a reminder, not an automatic upload. A hook has no model, so it cannot read
a session and decide what mattered, and no mechanical rule turns "this session" into rows of an
arbitrary schema — a grocery list and a CRM share nothing. Claude decides what to persist and
writes it through the memory tools it already has. Nothing is sent anywhere by the hook itself.

## Install

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

(For local development: clone `xmemory-ai/claude-code-plugin` and run `claude --plugin-dir .` from
its root — the `.claude-plugin/plugin.json`, `.mcp.json`, `skills/` and `hooks/` all live there.)

## Tools

### `xmemory` (memory)

The connect screen lets you choose exactly which to authorize; the **schema-management** group
starts unchecked.

**Core memory** (granted by default)

| Tool | What it does |
|---|---|
| `write_async` | Preferred write path — save create/update/delete intent in natural language; returns immediately. |
| `write` | Synchronous write; use only when you must read the same data back in the same turn. |
| `read` | Query the instance in natural language (lookups, aggregations, listings, traversals). |
| `write_status` | Diagnostic only — check once whether a specific async write landed. |
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

### `xmemory-admin` (instance management)

Global account/fleet surface — no instance binding. Use it to provision and manage instances.

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
Permission is granted to redistribute the plugin solely to distribute it through the Claude Code
plugin directory, and to install it solely to connect to the xmemory service; all other rights
are reserved. See [`LICENSE`](./LICENSE). Use of xmemory is governed by the
[Terms & Conditions](https://xmemory.ai/terms-and-conditions.html).
