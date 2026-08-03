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
