# Verifying the skills by hand

The hooks have `hooks/test_hooks.sh`. The two skills do not, and cannot in the same
sense: a skill is instructions to a model, so there is nothing to assert against
except a transcript. What follows is the walkthrough a reviewer or maintainer can run
to see whether each still does what it says — written as steps with observable
outcomes, not as prose about intent.

Run these against a scratch directory, never a real project.

## connect

1. **Discovery, admin authorized.** `/xmemory:connect` in a directory with no
   `.xmemory.json`. Expect: it lists your instances with names and purposes, and
   proposes tiers rather than asking cold.
2. **Discovery, admin *not* authorized.** Revoke the `xmemory-admin` connection and
   repeat. Expect: it says the connection is not authorized, points at `/mcp`, and
   offers `xmemcli org list instances --json` as the fallback. It must not invent an
   instance id, and must not stop without mentioning the fallback.
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
