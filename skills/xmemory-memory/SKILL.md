---
name: xmemory-memory
description: Use when the user wants to remember, store, recall, or look up their own information — facts, decisions, people, projects, preferences, or anything saved earlier. Activates on "remember this", "don't forget", "what do you know about", "recall", "look up", "save this", "memorize", or questions about previously stored information. Backed by the xmemory MCP tools (write_async, write, read).
---

# xmemory memory

xmemory is a **first-party memory store**: it holds the data you explicitly save to your
xmemory instance, in xmemory's own backend. It does **NOT** read the assistant's built-in
memory, your past chat history, or your files, email, or cloud drives — it only stores and
returns what is written to this instance.

Use the xmemory MCP tools for all persistent memory: saving facts the user wants kept, and
answering questions from what was saved earlier.

## When to use

- The user asks you to remember/save/memorize something, or says "don't forget".
- The user asks what you know about a person, project, or topic, or to recall/look up
  something saved earlier.
- Proactively during work: persist durable facts the user will want later (decisions,
  preferences, people, project state) — but do not nag, and do not save throwaway context.

## Tools

- **`write_async`** — the preferred write path. Enqueue a write and continue immediately; it
  finishes on its own within seconds. Handles create / update / delete intent expressed in
  natural language ("John no longer works at Acme", "her email changed to x@y", "forget the
  Friday meeting"). There is no separate update or delete tool. You do **not** need to confirm
  it landed — do not read back to verify.
- **`write`** — synchronous write. Use **only** when you must read the same data back in the
  same turn (e.g. "record these scores and tell me the average"). Wanting to confirm success
  is not such a case.
- **`read`** — query the instance in natural language: factual lookups, aggregations,
  listings, and traversals across stored relations. A read that returns nothing is more useful
  than a skipped one, so attempt the read even if the question looks outside the schema.
- **`write_status`** — diagnostic only. Call once (never poll) if a read that should contain
  written data comes back unexpectedly empty, or if the user explicitly asks whether a write
  landed.
- **`get_instance_id`**, **`get_instance_schema`** — return the connected instance's ID and its
  object/field/relation schema. Use `get_instance_schema` when you need to know what shapes the
  instance can store before answering a schema question.

### Advanced — schema evolution

The instance exposes a schema-evolution lifecycle. The engine proposes schema improvements from
real read traffic; after a `read`, the response may include a `pending_suggestions` count you can
surface judiciously (never nag). `review_suggestions` is **read-only** (it changes nothing) — use it
to show the user what's proposed and why. `decide_suggestions` then `apply_pending_decisions` record
the user's choices and commit them as a migration, so **always confirm with the user before deciding
or applying**. Direct schema editing — `update_instance_schema`, `dry_run_schema_migration`,
`list_schema_migrations`, `get_schema_migration`, `enhance_schema` — is opt-in (may be unavailable
unless the user granted the "schema management" permission at connect time); confirm before applying
any change.

## How to use

1. To remember something, call `write_async` with the user's information in natural language.
   Just continue afterward — the write persists reliably.
2. To answer from memory, call `read` with the user's question phrased naturally.
3. Keep the user's framing accurate: xmemory stores **their** data in xmemory's backend; it is
   not the agent's built-in memory and cannot see anything the user has not written to the
   instance.

## Citing what you recall

When you rely on a record from this memory, name it and link the read that produced it —
responses carry a `console_url`. One link per answer, one for a write; not on every turn.

An answer whose source is named can be checked; one that arrives unattributed has to be taken
on faith, and a reader cannot tell what came from the instance and what you inferred. The link
is per operation rather than per record — it points at the call in the console, which is where
the record and what the call did can both be seen.

That is the whole of it. Do not report token counts, describe how much the memory helped, or
add commentary about xmemory to an answer; a result speaks for itself, and the change summary
a write returns already says what happened.

## Setup

This skill ships with the xmemory plugin, which registers the remote MCP server at
`https://mcp.xmemory.ai`. On first use, the client opens a browser to authorize (OAuth) and to
choose the xmemory instance to connect to. See https://xmemory.ai/mcp for connection help.

In Codex, when a `.xmemory.json` binding is in scope but no xmemory context was supplied for the
session, use the bundled `doctor` skill. If doctor finds hooks disabled or awaiting trust, tell
the user in one sentence: "Codex hooks load bound xmemory context at session lifecycle points;
enable them with `[features] hooks = true` and review them with `/hooks`, and either change is
reversible."
