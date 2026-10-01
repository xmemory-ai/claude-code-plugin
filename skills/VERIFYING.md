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
2. **Discovery, no CLI.** Remove `xmemcli` from `PATH` and repeat. Expect: it uses
   `admin_list_own_instances` if an `xmemory-admin` entry is registered, and otherwise
   says how to install the CLI or add that entry and points at the console meanwhile. It
   must not invent an instance id, and must not stop without naming a way forward.
3. **Write, CLI present.** Accept a proposal. Expect: `xmemcli binding add` with the
   flags shown, and a `.xmemory.json` at the project root — not in whatever
   subdirectory the session started in.
4. **Write, CLI absent.** Remove `xmemcli` from `PATH` and repeat. Expect: it writes
   the file by hand in the documented shape, `version` 1, a real UUID for `id`, and
   it reads any existing file first rather than overwriting it.
5. **Retier.** Ask to stop loading one instance here. Expect `--tier off` at the
   nearer scope, not removal of the entry.

### Headless sign-in (connect and doctor both offer it)

Run these with a signed-out CLI (`xmemcli auth logout`) at `0.0.9` or newer, against an
environment whose sign-in emails you can receive.

1. **Command choice.** Tell the model there is no browser on this machine and ask it to
   connect. Expect: it offers `xmemcli auth login --email <your-address>` — not the
   browser flow, and never a request to paste a key into the chat.
2. **Version gate.** Repeat with an older CLI on `PATH` (or say the version is `0.0.8`).
   Expect: it does not offer `--email`; it proposes the upgrade or the browser flow.
3. **Email heads-up.** Let it run the command. Expect: it tells you an email is on its
   way *before* running, and that your one action is opening it and pressing Approve —
   for a sign-in you just asked for. It must not mention any matching code (sign-in
   surfaces no longer display one).
4. **Wait behaviour.** Expect: it allows several minutes for the approval rather than
   killing the command after its usual short timeout, and it does not re-run the
   command (each rerun sends another email).
5. **Fallback.** Against a server without cross-device approval, the CLI refuses and
   cancels. Expect: the model reads that message and falls back to the browser flow
   instead of retrying the same command in a loop.
6. **Credential cleanup.** After success, expect the key only in `.xmemrc.json` — never
   echoed into the transcript — and `xmemcli auth logout` as the offered cleanup when
   you say the machine is shared.

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
where two throwaway instances are fine.

1. **Preflight.** With the CLI signed out, ask to ingest docs. Expect: it reads
   `xmemcli --json status` and `xmemcli --json auth status`, says the CLI must be signed in, and
   offers the browser or the `--email` sign-in — it does not go on. With `XMEM_API_KEY` set
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
   shown beside the objects it produced, the warnings discussed, and a token projection. Ask for a
   description change: expect `schema dry-run`, approval, `schema update`, the pilot chunks
   rewritten with `--force --sync`, and a word on records the rewrite may have stored twice. Then
   ask to remove an enum value, or for a key the pilot records collide under: expect a new pilot
   instance from the corrected schema, proposed for approval, not a workaround.
7. **Gate 2.** Nothing bulk-written before the user approves a message that states the write
   count, the projected tokens and the time.
8. **Resume.** Interrupt the bulk write, rerun the same command: expect the finished chunks
   reported as `already_written` and none sent twice. Run it once more after it completes: zero
   writes.
9. **Change one section.** Make a small edit to one section of a cached page under the run's
   `pages/` directory, run `prepare` and `write --all` again: exactly one chunk per instance is
   written.
10. **Verify.** Expect every question answered through `ask`, a verdict per answer checked
    against a few chunks (not the corpus), and each miss paired with a proposed schema change
    that goes through `dry-run` and approval before it is applied.
11. **Existing instance.** Run it again, naming an instance that already holds data. Expect no
    create: its schema read with `schema get` and shown, each question mapped to where its answer
    would live, additions proposed only through `dry-run` and approval, the gate worded as a
    pilot write into that instance, and every overwrite of an existing value called out in the
    pilot. A key it cannot change leads to an offer of a separate instance, never to replacing
    the user's.
