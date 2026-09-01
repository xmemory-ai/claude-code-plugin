# Migrating a hand-wired xmemory setup

People wired xmemory into Claude Code before this plugin existed — usually hooks in
`~/.claude/settings.json` pointing at their own scripts. Installing the plugin on top
of that setup does **not** replace those hooks. Both run.

The result is duplicate work rather than an error, which is why it is easy to miss:
context injected twice at session start, the same reminder twice before compaction,
and roughly double the tokens for it.

## What overlaps, and what does not

Compare by **event**, not by whether a script mentions xmemory.

| Your hook | Plugin equivalent | What to do |
|-----------|-------------------|------------|
| `SessionStart` | SessionStart hook | Overlaps — keep one |
| `PreCompact` | PreCompact hook | Overlaps — keep one |
| `PostToolUse` | *none* | **Keep yours.** The plugin has no equivalent |
| `Stop`, `SessionEnd`, `UserPromptSubmit` | *none* | **Keep yours** |

Only the first two overlap. Anything else is doing something the plugin does not do,
and removing it loses that behavior outright.

## Which one to keep

The honest answer depends on what your script does, and "the plugin's" is not
automatically right.

**Keep the plugin's** when your hook does the generic thing — load the instances this
directory cares about, remind Claude to persist before compaction. The plugin version
is maintained, degrades properly when the CLI or credential is missing, and reads the
same `.xmemory.json` binding the `connect` skill writes.

**Keep yours** when the script does something the plugin cannot. The clearest case is
work tied to a *specific schema*: caching a todo list and writing it back as typed
plan steps, keying state to the current git branch, flushing named fields of an
instance you designed. The plugin's hooks are deliberately schema-agnostic, because a
bound instance might be a grocery list or an investor pipeline, so no rule turns a
session into rows of a particular schema. A hook that knows your schema will always do
more than one that cannot.

**Keeping both** is a legitimate choice if they genuinely do different things at the
same event — just know you are paying for both, and that both will inject.

## How to remove the overlap

Two ways, depending on which side you drop.

Drop **your** hook: edit `~/.claude/settings.json` (or the project's
`.claude/settings.json`) and delete that event's entry. Nothing else needs changing —
the plugin's hooks are registered by the plugin itself, not by your settings.

Drop **the plugin's** hooks while keeping its skills and MCP servers: set

```sh
export XMEMORY_DISABLE_HOOKS=1
```

Both hooks stand down; the `connect` and `doctor` skills and both MCP servers are
unaffected. Any non-empty value counts, following the `NO_COLOR` convention — including
`XMEMORY_DISABLE_HOOKS=0`, which disables them rather than enabling them. To turn the
hooks back on, unset the variable. Claude Code has no built-in way to switch off one hook
of an installed plugin, so this is the plugin's own opt-out — put it wherever your shell
environment is defined so it applies to every session.

It is all-or-nothing across the two hooks. If you want to keep one and drop the
other, say so in an issue; splitting it is cheap and has not been asked for yet.

## Checking

`/xmemory:doctor` reports hand-wired hooks it finds on overlapping events, and names
the file they live in. It does not change anything: which one to keep is a judgement
about what your script does, and it cannot read that from a command line.

## Upgrading from a version that bundled MCP entries

Versions before 0.4.0 registered two MCP entries of their own for everyone who installed the
plugin — `xmemory`, pointing at the shared root URL, and `xmemory-admin`. Anyone connecting
through a per-instance entry (the form `xmemcli instance setup` prints) never used them, yet the
client listed both as needing authentication in every session. Since 0.4.0 the plugin bundles no
MCP entry; connections are registered per instance instead.

After updating, both bundled entries disappear from `/mcp` on their own — a plugin's entries
live and die with the plugin. Nothing else changes:

- **A per-instance entry** (`xmemory-<id8>`, whether it runs `xmemcli mcp <id>` or points at
  `https://mcp.xmemory.ai/instance/<id>`) is yours, not the plugin's. It stays registered and
  authorized.
- **If you had authorized the `xmemory` entry an older version bundled** and relied on it,
  register that instance explicitly: `claude mcp add xmemory-<id8> -- xmemcli mcp <id>` with the
  CLI signed in, or the direct form — the README shows both. `xmemcli org list instances` lists
  your instances if you are unsure which one it was bound to.
- **If you used the admin tools over MCP**, add the admin entry yourself:
  `claude mcp add --transport http xmemory-admin https://mcp.xmemory.ai/admin` (Codex:
  `codex mcp add xmemory-admin --url https://mcp.xmemory.ai/admin`). The CLI covers the same
  operations without it.

## What does not need migrating

- **MCP server entries.** The plugin registers none, so an entry you added yourself — a
  per-instance `https://mcp.xmemory.ai/instance/<id>` URL, or an `xmemcli mcp <id>` command — is
  the connection, and nothing here duplicates it.
- **`.xmemrc.json`.** The CLI credential is unchanged and the plugin does not touch it.
- **Your own scripts and helper files.** The hooks touch exactly three things on disk:
  they test for `.xmemory.json` to see whether anything is bound, they run `xmemcli`,
  and they use a temporary file to capture its stderr. Precisely:

  - **SessionStart** existence-tests `.xmemory.json` in each ancestor directory; runs
    `xmemcli … context` and captures its stderr through one `mktemp` file, which it
    removes. Only when no `xmemcli` is installed at all does it read the contents of
    those files, grepping for `autoload` to decide whether an install hint is worth
    showing.
  - **PreCompact** does the existence test, then asks `xmemcli … binding list --json`
    which instances are engaged. That is a local file read — no credential, no
    network, no request — and if no client is installed it shows the nudge without
    reading anything further.

  Nothing else is read, written or removed.
