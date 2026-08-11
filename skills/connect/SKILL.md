---
name: connect
description: Use when the user wants to connect, bind, attach, or set up an xmemory instance for the current project — "connect xmemory", "which instances do I have", "bind my team knowledge instance here", "set up xmemory in this repo", "stop loading that instance here", or asks what xmemory knows about this working directory. Discovers the user's instances over MCP and records the choice in a local .xmemory.json binding.
---

# Connect an xmemory instance to this project

A **binding** records which xmemory instances an agent working in *this directory* should know
about, and how eagerly to engage each one. It lives in `.xmemory.json` and holds **no secrets** —
an instance id is not a credential — so the project-scope file is meant to be committed and
shared with the team.

Binding an instance is a local bookkeeping act. It does not grant access, move data, or change
anything on the server.

## Binding is not the same as connecting

Keep these two apart when talking to the user, because they are easy to conflate:

- A **binding** says "instances X and Y matter in this directory." It is a local file. It can
  name as many instances as you like.
- An **MCP connection** is what actually lets you read and write an instance's data. The bundled
  `xmemory` server entry holds **one** connection, bound to **one** instance chosen interactively
  in the OAuth sign-in screen.

So binding three instances does **not** make all three readable over MCP at once. To have several
live concurrently the user adds one named server per instance. The registration command differs
between Claude Code and Codex; identify the current client and show only its command rather than
making the user choose between client names.

**Check four states in order** — no CLI, an outdated CLI, a signed-in current CLI, and an
installed but signed-out current CLI have different fixes:

```bash
xmemcli --json status
```

One local call, contacting nothing, reports `version` and `authenticated` together:

- **`command not found`** → no CLI. Use the direct form at the bottom of this section.
- **`version` below `0.0.7`** → the client is too old: `mcp` arrived in 0.0.7, so registering
  the client form would leave a server that cannot start. Ask the user to run
  `uv tool install --upgrade xmemcli`, or use the direct form if they cannot upgrade.
- **`"authenticated": false`** → sign the CLI in: ask the user to run `xmemcli auth login`
  (browser), or — when `version` is `0.0.9` or newer — run the headless
  `xmemcli auth login --email <their-address>` on their behalf; see
  [Does the user need the CLI?](#does-the-user-need-the-cli) for how that approval works. On an
  older client the headless flag does not exist: offer the browser flow, or
  `uv tool install --upgrade xmemcli` first. One sign-in then serves this instance and every
  later client-form entry. Continue after it succeeds.
- **`"authenticated": true`** and `version` is at least `0.0.7` → go straight to the client
  form below.

`status` exits 0 whether credentials are present or absent: read the fields, never infer readiness
from the exit code.

**The client form** — the CLI supplies the credential itself. Use the command for the active
client and omit the other one from the user-facing answer:

```bash
# Claude Code
claude mcp add xmemory-work -- xmemcli mcp <work-instance-id>

# Codex
codex mcp add xmemory-work -- xmemcli mcp <work-instance-id>
```

`xmemcli mcp` is a transport, not a command a person runs: the client starts it, and it forwards
each frame to that instance with the key read from `.xmemrc.json`. Nothing is captured when the
entry is written, so it keeps working in later sessions without an exported environment variable.

Registering it before signing in loses nothing: the server reports the missing credential in its
failure line, and `xmemcli auth login` — browser, or its headless `--email` variant — fixes it
without changing the MCP entry. Checking first
simply avoids leaving the user with a connection that initially looks broken.

**The direct form** — when the CLI is absent or too old and cannot be upgraded. Again, show only
the active client's command:

```bash
# Claude Code
claude mcp add --transport http xmemory-work "https://mcp.xmemory.ai/instance/<work-instance-id>"

# Codex
codex mcp add xmemory-work --url "https://mcp.xmemory.ai/instance/<work-instance-id>"
```

This form signs in through the browser once per entry. In Claude Code, authorize the named server
with `/mcp` or `claude mcp login xmemory-work`. In Codex, use `codex mcp login xmemory-work`; `/mcp`
shows the resulting connection. It does not authorize itself merely because it was registered.

Keep the two conditions as separate commands, not one shell conditional. This skill is used on
POSIX shells and PowerShell, so choosing the applicable command in the instructions is portable
while a shell `if` is not. Use the same client-first/direct-fallback sequence everywhere so the
console and plugin cannot produce differently configured entries.

When you bind an instance the user clearly wants live alongside another, offer to add that entry
too — you have the id already.

## When to use

- The user asks to connect / bind / set up xmemory for a project or repo.
- The user asks which instances they have, or which ones this project uses.
- The user wants an instance loaded automatically every session, or wants to stop that.
- The user wants to unbind an instance.

Do **not** use this skill to read or write instance data — that is `xmemory-memory`.

## Tiers

Every bound instance has a tier, which is the whole point of the binding:

| Tier | Meaning |
|------|---------|
| `autoload` | Pull this instance's context at the start of every session in this directory. |
| `available` | **Default.** Do not preload; engage it when the work actually matches. |
| `off` | Bound but dormant. Use this to silence an instance inherited from a wider scope. |

Recommend `autoload` only for instances whose content bears on *most* work in the directory
(team conventions, project state). Everything else should be `available` — an autoloaded
instance spends context on every single session whether or not it is relevant.

## Scopes

Two files, merged like git config, with the nearer one winning field by field — with one
exception. A `tier: off` set at **user** scope is reapplied after the merge, so a project
binding cannot raise an instance the user silenced. If a project `autoload` appears to have
no effect, the tier has to change at user scope; retiering the project file will not do it.

- **project** — `.xmemory.json` at the project root (the session root; the work-tree root in a
  checkout). Committed; shared with the team.
- **user** — `~/.xmemory.json`. Private to this machine; applies everywhere.

Default to **project** scope when in a repository, and **user** scope for an instance that is
personal to the user rather than to the codebase. Ask if it is genuinely ambiguous.

**One exception: when the directory being loaded *is* the home directory.** A devcontainer or CI
image that clones into `$HOME` produces this, and so does opening a session in the home directory
itself. There the user's location and the project's location are the same place, so there is no
distinct user scope and every file found is project scope — which is what they are. Working in
`~/projects/anything` is unaffected, including on a machine whose home directory is versioned,
because `$HOME` is then not the directory being loaded.

Either way `binding add` refuses to *infer* a project target that resolves onto the user's file,
rather than write it while calling it something else: pass `--binding-dir` to name where the
project binding belongs, or `--scope user` if the user's own file is genuinely what was meant.

## How to connect

### 1. Discover the user's instances

Call the **`admin_list_own_instances`** MCP tool. This needs no CLI — it uses the same OAuth
connection as the rest of the plugin. If it fails because the admin connection is not authorized,
tell the user how to authorize it (`/mcp`) and offer the CLI listing below as the fallback —
`xmemcli org list instances --json` reaches the same data through a credential the user
already has. Stop only if neither is available; never fall back to guessing instance ids.

If `xmemcli` is installed the equivalent is `xmemcli org list instances --json`, which is useful
when the user is already authenticated there but has not authorized the admin MCP connection.

If the user has no instances, say so and point them at the Console to create one. Do not invent
a binding for an instance that does not exist.

### 2. Propose a binding

Show the instances with their names and descriptions and let the user choose which to bind and at
what tier. Suggest tiers rather than asking cold — you can usually tell from an instance's purpose
and the directory you are in whether it is `autoload` material.

Capture, per instance:

- `id` — required, exactly as returned by the server.
- `name`, `purpose` — cached from the server so the binding reads well offline. Advisory only.
- `tier` — as above.
- `engage` — short free-text cues for when this instance is worth engaging, e.g.
  `"a team convention is learned or corrected"`. Most useful on `available` instances, which
  otherwise have nothing to trigger them.

### 3. Write it

**If `xmemcli` is on PATH**, prefer it — it validates the file and picks the right target path.

Resolve the project root once and use it for every command in this section, quoted:

```bash
project_root="$CLAUDE_PROJECT_DIR"      # or the repository root you determined above

xmemcli binding add "<instance-id>" \
  --binding-dir "$project_root" \
  --name "Team Knowledge" \
  --purpose "shared dev conventions" \
  --tier autoload \
  --engage "a team convention is learned or corrected" \
  --scope project
```

**Always pass `--binding-dir "$project_root"`** — for `add`, `remove`, `list` and retiering
alike. Without it the CLI resolves from its own working directory, so an agent that has moved
into a subdirectory writes the binding there; the session-start hook is rooted at the project
root and only walks *upward*, so it never sees it and autoload stays silent while connect
reports success. The same applies when you *check* your work: a `binding list` run from a
subdirectory can show a stale descendant binding that the hook will never load, which reads as
confirmation of something that is not true.

**Quote every substitution.** A project path with a space in it — `/tmp/My Project`,
`~/Library/Mobile Documents/…` — splits into two arguments unquoted, and the CLI exits 2 with
`unrecognized argument(s): Project`.

`binding add` is an upsert: it only writes the flags you pass, so re-running it with just
`--tier off` retiers an instance without wiping its cached name.

```bash
xmemcli binding list --binding-dir "$project_root"                        # the merged result
xmemcli binding remove "<id>" --binding-dir "$project_root" --scope project   # unbind
```

To silence an instance that comes from a wider scope, bind it locally with `--tier off` rather
than removing it.

**If `xmemcli` is not installed**, write the file directly — the format is stable and versioned:

```json
{
  "version": 1,
  "instances": [
    {
      "id": "00000000-0000-4000-8000-000000000000",
      "name": "Team Knowledge",
      "purpose": "shared dev conventions",
      "tier": "autoload",
      "engage": ["a team convention is learned or corrected"]
    }
  ]
}
```

Rules when writing it by hand:

- **`id` must be the instance's UUID**, exactly as the server returned it. The id in the
  example above is a placeholder — substitute the real one from discovery. Not a name, not a
  slug you invented. The API takes UUIDs, so any other value produces a binding that looks
  correct in the file and never loads anything. `xmemcli binding add` refuses a non-UUID for
  the same reason; nothing validates a hand-written one.
- Read any existing file first and **merge into it** — never overwrite a binding you did not read.
- Project scope goes at the **project root** — the directory the session is rooted at, not
  whatever subdirectory you happen to be in. In a git checkout that is `git rev-parse
  --show-toplevel`; without one it is simply the project folder. Do not require a repository:
  plenty of work an agent does is not source code, and a binding is not a version-control
  artefact. User scope is exactly `~/.xmemory.json`.
- `version` must be `1`. `id` is required; every other field is optional.
- `tier` must be one of `autoload`, `available`, `off`.
- Unknown keys are rejected on read, so do not add fields that are not listed here.
- One entry per instance id.

## Does the user need the CLI?

**No — not for this.** Binding works over MCP alone, and the instance context that arrives with the
MCP connection works without any CLI.

`xmemcli` adds the session-start half: it is what lets an `autoload` instance actually pull its
context at the start of a session, because a session-start hook is a separate process that cannot
reach the MCP OAuth token and needs its own credential.

So: if the user binds anything as `autoload`, mention once that the CLI is what makes autoload
take effect, and offer the install:

```bash
uv tool install xmemcli    # or: pip install xmemcli
xmemcli auth login
```

When there is no browser on this machine — or the user would rather not click through the
Console — run the headless variant on their behalf (it needs `xmemcli` `0.0.9` or newer;
upgrade older clients first):

```bash
xmemcli auth login --email <their-address>
```

The CLI reports the email is on its way and waits. The user's single action is opening the
sign-in email and pressing **Approve** — only for a sign-in they just asked for. The
command blocks until the approval arrives (up to ten minutes), so run it with a generous
timeout and tell the user before starting it that an email is on its way;
`--timeout <seconds>` shortens the wait. The
credential is written straight to the CLI's own store and is never printed, so it never
enters the conversation. If the CLI reports that the server offered no cross-device
approval, fall back to the browser flow above.

Mention it once, at that moment. Do not bring it up when everything is bound `available`, do not
repeat it in later sessions, and never block the binding on it — a binding written today starts
working the moment the CLI shows up.

## What committing a binding means

A project-scope `.xmemory.json` is committed, so it travels with the repository. When a teammate
opens a session there, the instances it names are fetched **with that teammate's credential** —
the server only returns what their own key can already reach, so nobody sees anyone else's data,
but each of them spends their own quota and gets that content in their session.

**Never commit `.xmemrc.json`.** It sits in the same directory and looks like a companion file,
but it holds a plaintext API key. `xmemcli context` refuses to fetch with a credential found
inside a checkout precisely because a committed one lets the repository choose the key and the
server for an automatic fetch — so committing it both exposes the key and disables preloading.
Only `.xmemory.json` is shared.

Two consequences worth saying out loud when you write one:

- Bind at `autoload` only what genuinely belongs to everyone working in that repository. Anything
  personal belongs in `~/.xmemory.json` at user scope.
- An instance a teammate has not listed for themselves is flagged in their session as having come
  from the project rather than from them. That is deliberate — a cloned repository should not be
  able to pull someone's instances into a session unannounced — so expect it and do not treat it
  as an error.

## After binding

Confirm what changed in one line per instance — id, name, tier, and which file was written — so
the user can see whether it landed in the committed project file or their private user file. If it
went to project scope, remind them it is a new file to commit.
