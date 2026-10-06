# Verifying the skills by hand

The hooks have `hooks/test_hooks.sh`, and the ingest script has
`skills/ingest-docs/scripts/test_ingest.py`. The skills themselves do not, and cannot in the
same sense: a skill is instructions to a model, so there is nothing to assert against
except a transcript. What follows is the walkthrough a reviewer or maintainer can run
to see whether each still does what it says — written as steps with observable
outcomes, not as prose about intent.

Run these against a scratch directory, never a real project.

## connect

1. **Discovery, CLI signed in.** `/xmemory:connect` in a directory with no
   `.xmemory.json`. Expect: it runs `xmemcli org list instances --json`, lists your
   instances with names and purposes, and proposes tiers rather than asking cold.
2. **Discovery, no CLI.** Remove `xmemcli` from `PATH` and repeat. Expect: it offers to
   install and sign in the CLI first, as in [No CLI on the machine](#no-cli-on-the-machine).
   Decline the install. Expect: it uses `admin_list_own_instances` if an `xmemory-admin` entry
   is registered, and otherwise says how to get one and points at the console meanwhile. It
   must not invent an instance id, and must not stop without naming a way forward.
3. **Write, CLI present.** Accept a proposal. Expect: `xmemcli binding add` with the
   flags shown, and a `.xmemory.json` at the project root — not in whatever
   subdirectory the session started in.
4. **Write, CLI cannot be installed.** Remove `xmemcli` from `PATH`, repeat, and decline the
   install it offers. Expect: it writes the file by hand in the documented shape, `version` 1,
   a real UUID for `id`, and it reads any existing file first rather than overwriting it.
5. **Retier.** Ask to stop loading one instance here. Expect `--tier off` at the
   nearer scope, not removal of the entry.

### No CLI on the machine

This changes the machine, not just a scratch directory: it installs a user-level CLI, writes
`~/.xmemrc.json` and adds an MCP entry. Before starting, write down what is there now —
`xmemcli --json status` (or "command not found") and `claude mcp list` / `codex mcp list` — so
the teardown below can put it back. Use a throwaway instance, and an account whose sign-in emails
you can receive. Then uninstall the CLI with `uv tool uninstall xmemcli`.

1. **Install, not the direct form.** Ask to connect an instance. Expect: it installs the CLI with
   `uv tool install --upgrade xmemcli`, then runs
   `xmemcli auth login --rc-dir "$HOME" --email <your-address>`, and registers the client form.
   After you approve the email, the key is in `~/.xmemrc.json`, and there is no `.xmemrc.json`
   in the scratch directory.
2. **Off `PATH`.** If the install warned that its directory is not on `PATH`, expect: it
   suggests `uv tool update-shell`, and the registered entry names the absolute path the install
   printed, in double quotes, not a bare `xmemcli`.
3. **Install fails.** Uninstall again, and make the install impossible: run the client with a
   `PATH` that has neither `uv` nor `pipx`, or tell it installing tools is not allowed here.
   Expect: it offers the direct form, and says before you meet the page that it will ask for an
   xmemory API key from the Console's **API Keys** page.
4. **Doctor without the CLI.** With the CLI still uninstalled and an `autoload` binding in the
   scratch directory, run `/xmemory:doctor`. Expect: check 4 names
   `uv tool install --upgrade xmemcli` and the home email sign-in, and the other four checks are
   still reported.
5. **Session-start hint.** Still without the CLI, start a new session in the scratch directory.
   Expect one hint naming `uv tool install --upgrade xmemcli` and
   `xmemcli auth login --rc-dir "$HOME" --email <address>`. `hooks/test_hooks.sh` pins the same
   text.

**Teardown.** Remove the entry the walkthrough added (`claude mcp remove xmemory-<id8>` or
`codex mcp remove xmemory-<id8>`) and the scratch directory's `.xmemory.json`. Run
`xmemcli auth logout --rc-dir "$HOME"`, and revoke the key it held on the Console's **API Keys**
page. If the machine had no CLI before, run `uv tool uninstall xmemcli`; otherwise reinstall the
version you wrote down and sign it back in. Then compare `xmemcli --json status` and the MCP list
with what you wrote down at the start.

### Email sign-in (connect and doctor both lead with it)

Run these with a signed-out CLI (`xmemcli auth logout`) at `0.0.9` or newer, against an
environment whose sign-in emails you can receive.

1. **Command choice.** Ask it to connect. Expect: it asks for your email address and offers
   `xmemcli auth login --rc-dir "$HOME" --email <your-address>` — the browser flow only as a
   fallback, and never a request to paste a key into the chat.
2. **Version gate.** Repeat with an older CLI on `PATH` (or say the version is `0.0.8`).
   Expect: it upgrades with `uv tool install --upgrade xmemcli` before signing in, and never runs
   `--email` on that version.
3. **Email heads-up.** Let it run the command. Expect: it tells you an email is on its
   way *before* running, and that your one action is opening it and pressing Approve —
   for a sign-in you just asked for. It must not mention any matching code (sign-in
   surfaces no longer display one).
4. **Wait behaviour.** Expect: it allows several minutes for the approval rather than
   killing the command after its usual short timeout, and it does not start a second attempt
   while the first is waiting (each attempt sends another email).
5. **Fallback.** Against a server without cross-device approval, the CLI refuses and
   cancels. Expect: the model reads that message and falls back to the browser flow
   instead of retrying the same command in a loop.
6. **Credential cleanup.** After success, expect the key only in `~/.xmemrc.json` — not in the
   scratch directory, and never echoed into the transcript — and
   `xmemcli auth logout --rc-dir "$HOME"` as the offered cleanup when you say the machine is
   shared.
7. **A project credential.** In the scratch directory, sign in once without `--rc-dir`, so
   `xmemcli --json status` reports an `rc_file` there, then ask it to connect. Expect: it signs
   in at home anyway with `--rc-dir "$HOME"`, and asks you to delete the scratch directory's
   `.xmemrc.json` afterwards. Delete it when the step is done.

## doctor

`/xmemory:doctor` in a directory with a binding, then check it reported on **all
five** independently — a report that stops at the first failure is the failure mode
this skill exists to avoid.

1. MCP server registered — says which tools it can see.
2. Connection authorized — calls `get_instance_id` and reports the instance.
3. Binding present — lists instances and tiers, and says only `autoload` preloads.
4. CLI installed *and* signed in — treated as two states with different fixes.
5. Hand-wired hooks on the same events — names the file if it finds any.

Then break one thing at a time and confirm the report still covers the others: sign out
(`xmemcli auth logout`), rename `.xmemory.json`, and set `XMEMORY_DISABLE_HOOKS=1` — that
last one must be *named* by check 5, since every other check can pass while nothing loads.

Check 4 also prices the pack. With something bound `autoload`, expect an approximate token
figure against the budget, phrased as what a session start *would* inject rather than what
this session was given. Bind several instances, or one whose live state is long, and run it
with `--max-tokens` low enough to force a cut: the report should name the instance and the
section the budget went to and offer retiering to `available` — not a reinstall. When
`universal_rules` is not `null`, the per-instance figures sum to less than the total by exactly
that block, which is carried once for the whole response. On a CLI old enough to return no `packs`, expect the totals alone and no suggestion that
anything is broken.

## ingest-docs

Run it in a scratch directory against a small public docs site that serves Markdown (this
product's own `https://xmemory.ai/llms.txt` works), with 10–20 questions about it, on an account
where several throwaway instances are fine (the pilot step creates two more, which you then
delete).

1. **Preflight.** With the CLI signed out, ask to ingest docs. Expect: it reads
   `xmemcli --json status` and `xmemcli --json auth status`, says the CLI must be signed in, and
   offers `xmemcli auth login --rc-dir "$HOME" --email <your-address>`, with the browser only as
   a fallback — it does not go on. With `XMEM_API_KEY` set
   instead of a sign-in, it goes on without asking. With a CLI older than `1.5.1`, it proposes
   the upgrade.
2. **One question at a time.** Expect where the docs go, the question list and the doc source
   asked in separate messages, then `discover` output shown as groups, and a scope question.
3. **Script, not context.** Across the whole run, the transcript should show the agent reading
   only the five sample chunks, the pilot chunks and the answers — never a page or the chunk
   directory wholesale.
4. **Schema.** Expect a tight instance for lookup questions and, when there are how/why
   questions, a broad one; XMD written by the agent after reading the XMD guide, passing
   `xmemcli xmd validate`, and shown as tables. Every key sits on a field marked
   `required: true` that the docs state wherever the thing is mentioned; objects without such a
   field have `primary_key: []`.
5. **Gate 1.** Nothing is created before the user approves "create and pilot".
6. **Pilot.** Expect `write --sync` on 5–10 chunks covering every question group, each chunk
   shown beside the objects it produced, the warnings discussed, and a token projection in
   xmemory tokens. Ask for a description change, then for a removed enum value: each time expect
   a new pilot instance from the corrected schema, proposed for approval, the pilot chunks written
   to it with `--sync`, and the superseded instance named with its id for you to delete.
7. **Gate 2.** Nothing bulk-written before the user approves a message that states the write
   count, the projected xmemory tokens and the time — with an offer of `--concurrency 8` when
   the corpus runs to several hundred chunks.
8. **Resume.** Interrupt the bulk write, rerun the same command: expect the finished chunks
   reported as `already_written` and none sent twice. Run it once more after it completes: zero
   writes.
9. **Change one section.** Make a small edit to one section of a cached page under the run's
   `pages/` directory, run `prepare` and `write --all` again: exactly one chunk per instance is
   written.
10. **Verify.** Expect every question answered through `ask`, each answer shown as the records
    the read selected, a verdict per answer checked against a few chunks (not the corpus), and
    each miss paired with a proposed schema change
    that goes through `dry-run` and approval before it is applied.
11. **Existing instance.** Run it again, naming an instance that already holds data. Expect no
    create: its schema read with `schema get` and shown, each question mapped to where its
    answer would live, additions applied only through `dry-run` and approval, a plain statement
    that records the docs name are updated in place, the gate worded as a pilot write into that
    instance, and every value the pilot replaces shown before the bulk write. A key it cannot
    change leads to an offer of a separate instance, never to replacing yours.
