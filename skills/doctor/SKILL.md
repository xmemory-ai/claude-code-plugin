---
name: doctor
description: Use when xmemory is not behaving as expected — memory tools missing or erroring, "why didn't my instance load", context not appearing at session start, after connecting or installing, or when the user asks to check, diagnose, verify or troubleshoot their xmemory setup. Reports which parts of the setup are working and what to do about the parts that are not.
---

# Diagnose an xmemory setup

Five cross-client things have to line up, and they **fail independently**. Check all
five before concluding anything — a report that stops at the first failure sends
people to fix the wrong thing, and "xmemory is broken" almost always means exactly
one of these is missing while the rest are fine. In Codex, run the two Codex-only
checks afterward.

| # | Check | Without it |
|---|-------|------------|
| 1 | MCP server registered | No memory tools at all |
| 2 | MCP connection authorized | Tools present but every call fails |
| 3 | Binding present | Nothing to preload; tools still work |
| 4 | `xmemcli` installed **and** signed in | Bound instances are not preloaded at session start |
| 5 | Hooks not disabled, and not duplicated | Silence despite 1-4 passing, or context twice |

Run all five, then report. Do not stop early.

## 1. Is the MCP server registered?

Look at the tools available to you. If `read` / `write_async` (or their namespaced
forms) are present, the `xmemory` server is registered. `admin_list_own_instances`
means the separate `xmemory-admin` server is registered too — the two are registered
and authorized independently, so one working says nothing about the other.

If no xmemory tools exist at all, the plugin is not installed or the client has not
reloaded.

- Claude Code: `/plugin marketplace add xmemory-ai/claude-code-plugin`, then
  `/plugin install xmemory@xmemory-ai`.
- Codex: `codex plugin marketplace add xmemory-ai/claude-code-plugin`, then
  `codex plugin add xmemory@xmemory-ai`.

Start a new session after installation.

## 2. Is the connection authorized?

Registered is not connected. Call a cheap read-only tool — **`get_instance_id`** is
the right one: it takes no arguments, changes nothing, and costs nothing.

- It returns an id → authorized, and you now know which instance is connected.
- It fails with an auth error → **inspect how that entry connects before advising**, because
  the two forms have different fixes:

  - **Stdio with `command: xmemcli`** → it authenticates with the CLI credential. Run
    `xmemcli --json status`, which reports both likely causes locally. A `version` below `0.0.7`
    predates the `mcp` command, so upgrade with `uv tool install --upgrade xmemcli`. If the version
    is current but `authenticated` is false, sign the CLI in — `xmemcli auth login`, or on
    `0.0.9`+ the headless `xmemcli auth login --email <address>`. Either fix leaves the MCP
    configuration intact. If the version is current and credentials are present, the key may have
    been revoked; the server's failure line distinguishes that case.
  - **Streamable HTTP with no bearer token or `Authorization` header** → it authenticates in
    the browser. In Claude Code, use `/mcp` or `claude mcp login <server-name>`. In Codex, use
    `codex mcp login <server-name>`; `/mcp` shows the connection afterward.

  For Codex, `codex mcp get <server-name> --json` reports these as
  `transport.type: "stdio"` and `transport.type: "streamable_http"`. For Claude Code, inspect the
  named MCP entry and distinguish `"command": "xmemcli"` from `"type": "http"`. Never print
  environment values, headers, or credential files while inspecting an entry.

Report the connected instance id. Users frequently have several instances and are
surprised by which one the connection is bound to — one server entry holds one
connection, chosen at sign-in for the bundled browser entry and fixed by the instance id in a
per-instance entry.

## 3. Is anything bound to this directory?

Resolve the project root once and pin every command to it — the directory the session is
rooted at, which is what the hook passes. Your working directory may differ from it, and a
stale binding in a descendant will otherwise make this check report a healthy setup the real
hook cannot see. Quote it: a path containing a space splits into two arguments and the CLI
exits 2 with `unrecognized argument(s): …`.

```bash
project_root="$CLAUDE_PROJECT_DIR"
xmemcli binding list --binding-dir "$project_root"
```

If `xmemcli` is installed, that gives the merged view and the files it came from. Otherwise look for `.xmemory.json` at the project root — the directory the
session is rooted at, which in a checkout is the work-tree root — and in the user's home
directory. Do not report a missing repository as the problem; a binding does not need one.

Nothing bound is a perfectly normal state, not a fault — the memory tools work
without any binding. It only means nothing is preloaded at session start. Offer
`/xmemory:connect` rather than reporting an error.

If something **is** bound, say which instances and at what tier, and note that only
`autoload` entries are preloaded. An instance bound `available` that the user expected
at session start is the single most common "why didn't it load" — and the fix is a
tier change, not a reinstall.

## 4. Is the CLI installed and signed in?

Two separate states; check both, because the fix differs:

```bash
command -v xmemcli        # installed?
xmemcli --json status     # version and whether local credentials are present
```

- Not installed → `uv tool install xmemcli` (or `pip install xmemcli`).
- Installed but not signed in → `xmemcli auth login` (browser), or headless on `0.0.9`+:
  `xmemcli auth login --email <address>` — the CLI waits while the user's one action is
  approving the sign-in email for the attempt they just started.

This is **only** needed for preloading bound instances at session start, because that
happens in a hook — a separate process that cannot reach the MCP connection's OAuth
token and needs its own credential. Everything else works without it. Say so plainly
rather than presenting a missing CLI as a broken installation.

If both are in place, `xmemcli context --text --binding-dir "$project_root"` prints exactly
what a session start would inject — same root as the hook, or the answers diverge. Empty output means nothing is tiered `autoload` — which is the correct
result, not a failure.

Check its **stderr** too, which is where it reports what it declined to do. At session start
the hook forwards that line to the user as a `systemMessage` rather than into the model's
context, so running the command by hand is how you see it in full.

**Read those messages as written — do not match them against a list here.** The CLI's own text is
the contract, and it says what happened and usually what to do; a catalogue of exact substrings
in this file is a second copy of that contract, and a reworded message would silently stop being
recognised. What follows is only the handful whose *meaning* is regularly misread, or whose fix
is not in the message. Anything else: take the CLI at its word.

- **"…you have not listed yourself"** — not a fault, and the one most often reported as one. It
  means the project's binding names instances the user has not bound personally, which is the
  deliberate signal that a *commit* introduced them. It fires for a binding the user committed
  themselves too, because git can say a commit introduced an id but not whose commit it was, and
  answering that on every session start would cost an identity lookup. The escape is not in the
  message: bind the instance at user scope as well —
  `xmemcli binding add "<id>" --scope user --binding-dir "$project_root"` — after which it counts
  as the user's own and the notice goes quiet, here and everywhere else.

- **A refusal about the file's *kind*** — a symbolic link, more than one hard link, not a regular
  file. These read as bugs and are the opposite: a binding must be an ordinary file the project
  owns, because following a link would let a repository aim the reader at any file the user can
  read and have its contents described back in the error. Look at what the path actually is
  before assuming a fault.

- **Anything about a budget or a count** — too many `autoload` entries, or `--max-tokens` set
  below what the instances need. The message names the figure required; the part that is not in
  it is the rule of thumb, roughly 160 tokens per instance loaded. Raise the number, or retier
  the instances that matter least to `available`.

- **"written by a newer xmemory"** — a teammate committed a binding from a newer CLI. Upgrade
  (`uv tool install --upgrade xmemcli`). The file is deliberately not partially parsed: an old
  build silently ignoring a field a new one relies on is the worse failure.

- **A 4xx with the server's own explanation appended** — the request was wrong, and for a budget
  or instance-count mistake the server names the exact number needed. A 5xx is reported *without*
  its body on purpose: an error body carries stack traces and internal paths into a session
  transcript.

- **"xmemory returned no context"** — the request succeeded and produced nothing usable, usually
  because every requested instance was unavailable to this credential. `xmemcli context --json`
  shows the `unavailable` list with the server's reason per instance, which the one-line stderr
  message cannot.

- **".xmemrc.json in this directory was not used"** — informational, and the rule rather than a
  fault. Session-start context resolves its credential from the user and nowhere else: an
  `.xmemrc.json` sitting in a project directory is never a candidate, because that file could
  have arrived with the project, and this command runs unattended with its output going into the
  session. Other commands still honour a project-local credential when a person runs them.

  **The file itself is still worth a look, though.** It holds a plaintext API key. Run
  `git log -- .xmemrc.json`: if it is in the history, that key is exposed to everyone with
  repository access and must be **revoked and reissued** — deleting the file does not undo it.
  Then remove it from the working tree and add `.xmemrc.json` to the repository's `.gitignore`.

- **"…cannot tell your own .xmemrc.json from one the directory supplied"** — the one case that
  cannot be decided. When the directory being loaded *is* the home directory, a credential in it
  is both the user's own and the project's, and they are the same file; versioned dotfiles and an
  archive unpacked as `$HOME` are indistinguishable from the outside. So it says so instead of
  guessing.

  Fix: `export XMEM_API_KEY=…` in the shell profile, which is trusted because the caller chose it
  rather than the filesystem implying it. This only affects sessions rooted at the home directory
  — working in `~/projects/anything` needs nothing, because `$HOME` is then not the directory
  being loaded.

### What the pack costs

The sizes are part of the same command's structured output rather than a separate mode:
`xmemcli context --json` carries `estimated_tokens` against `max_tokens` for the whole
injection, plus a `packs` entry per instance with `truncated` and a per-section breakdown.
`universal_rules` is the operating-rules block, carried once for the whole response rather than
inside any instance — so when it is not `null`, the per-instance figures deliberately sum to less
than the total, and the difference is it.
The default rendering prints the pack alone because that is what a session-start hook pipes
into the session. Report the numbers as part of this check, and only here — an agent
volunteering token figures during ordinary work is noise nobody asked for.

Two things it settles that nothing else can:

- **A pack at or near `max_tokens` with `truncated` true** means the budget ran out and content
  was cut. The fix is retiering the instances that matter least to `available`, not reinstalling
  anything. The breakdown names which instance and which section paid for it.
- **One instance dominating the total** is usually a long live-state section, which is the only
  part with no natural length limit — it is a reader's answer about current data.

Say what it is: *approximately* this many tokens, and what a session start here **would** inject
rather than what this session was given. The command re-renders the pack; it cannot see what the
hook actually injected earlier, the binding may have changed since, and re-rendering pays for a
fresh live-state read. The figures are estimated from text length, not counted by a tokenizer, and
the ratio behind them is calibrated on English — a pack of CJK text costs more than it reports.

An older CLI returns the totals without `packs`: report the totals and say the breakdown needs a
newer `xmemcli` (`uv tool install --upgrade xmemcli`). A `null` `universal_rules` is a different
thing and not a CLI problem — that server does not send the block, and upgrading anything locally
will not change it. Present neither absence as a fault.


## 5. Are the hooks switched off, or duplicated?

**First: `echo "${XMEMORY_DISABLE_HOOKS:-<unset>}"`.** If it is set to anything non-empty, both
bundled hooks exit immediately and *every other check above can pass while nothing is injected* —
which is the exact "why didn't my context load?" symptom this skill exists to explain. Report the
value and where it came from (shell profile, `.envrc`, the client's own env settings); do not
change it, since someone set it deliberately.

Then: is anything hand-wired on the same events?

People wired xmemory into Claude Code before this plugin existed. Those hooks are not
replaced by installing it — both run, so context is injected twice at session start
and the compaction reminder fires twice. It looks like working software, just at
double the tokens, which is why nobody notices.

Read the `hooks` block of each settings file that exists:

```
~/.claude/settings.json          ~/.claude/settings.local.json
.claude/settings.json            .claude/settings.local.json
```

Report an entry only when **both** are true: it is registered on `SessionStart` or
`PreCompact` (the only two events this plugin uses), and its command does not mention
`CLAUDE_PLUGIN_ROOT` — that variable is how the plugin's own hooks are spelled, so it
distinguishes them from hand-written ones. Hooks on any other event (`PostToolUse`,
`Stop`, `UserPromptSubmit`) are doing something the plugin does not do; leave them
alone and do not mention them as problems.

Name the file and the event, then point at `MIGRATION.md`. **Do not recommend
deleting anything**, and do not edit a settings file unless the user asks. Which side
to keep depends on what the script does: a hook that knows a specific schema — writing
typed plan steps, keying state to a git branch — does more than the plugin's
deliberately schema-agnostic version ever can, and telling someone to remove it would
quietly cost them that.

## Codex only: are hooks enabled and trusted?

Run `codex features list` and find the effective state of `hooks`.

- `hooks ... true` means lifecycle hooks are enabled.
- `hooks ... false` means plugin hooks are loaded but cannot run.

Enabling hooks does not trust a plugin's commands. `/hooks` lists every discovered
definition; a new or changed non-managed hook is skipped until the user reviews and
trusts its current hash there. Do not use `--dangerously-bypass-hook-trust` as a setup
shortcut.

When either state explains missing context, tell the user in one sentence: "Codex
hooks load bound xmemory context at session lifecycle points; enable them with
`[features] hooks = true` and review them with `/hooks`, and either change is
reversible."

## Codex only: is the global fallback active?

This skill includes [`scripts/manage_codex_agents.sh`](scripts/manage_codex_agents.sh).
Resolve that linked resource to its absolute installed path, then run its `status`
action. It checks the active Codex home (`$CODEX_HOME`, otherwise `~/.codex`)
without changing anything:

```bash
sh <resolved-manager-path> status
```

If the fallback is absent, offer the `install` action. It adds or refreshes one marked
block in `AGENTS.md`, preserves everything outside the markers, and is idempotent.
Installing global instructions is a user-level configuration change, so do not run it
until the user asks for the fix:

```bash
sh <resolved-manager-path> install
```

The inverse action removes only that block:

```bash
sh <resolved-manager-path> remove
```

A non-empty `AGENTS.override.md` shadows the ordinary global `AGENTS.md`; the script
reports that state instead of claiming the fallback is active.

## Reporting

Give a short line per check, then the fixes in the order they should be applied
(registration before authorisation, installation before sign-in). Prefer naming the
one thing that is actually wrong over listing everything that is right.

Two states worth calling out explicitly, because they read as breakage and are not:

- **Tools work, no CLI.** Fully functional for reading and writing memory; only
  session-start preloading is unavailable.
- **Everything installed, nothing preloaded.** Almost always an `available` tier
  rather than `autoload`, or an empty binding.

Never print an API key, a token, or the contents of `.xmemrc.json`. `xmemcli auth
status` deliberately shows only the account and a key prefix — report that, and
nothing more.
