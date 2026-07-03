# xmemory — Claude Code plugin

Persistent, schema-structured memory for Claude. This plugin registers the **xmemory remote
MCP server** so Claude Code can save and recall your own data on demand, and ships a skill that
tells Claude when to use it.

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
its root — the `.claude-plugin/plugin.json`, `.mcp.json`, and skill live there.)

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
