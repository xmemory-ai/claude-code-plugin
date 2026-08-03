#!/bin/sh
# xmemory PreCompact hook — the last moment to persist what this session learned.
#
# Compaction is where durable facts are quietly lost: what the agent worked out
# over a long session is about to be summarised away, and anything not written
# down is gone. This is the one point where a reminder is worth its tokens.
#
# It is a nudge, not a transfer, and that is a deliberate limit rather than an
# unfinished feature. A hook has no model: it cannot read a session and decide
# what was durable. Nor is there a mechanical mapping to fall back on — a bound
# instance may hold a grocery list, an investor pipeline or a CRM, and no rule
# turns "this session" into rows of an arbitrary schema. Only the agent knows
# what it learned and which of its instances that belongs in, so the agent is
# what gets asked. The writing happens through the memory tools it already has.
#
# PreCompact is the only event where this makes sense. SessionEnd is too late —
# the agent cannot act on it — and Stop fires every time the agent finishes
# replying, which would nag on every turn.
#
# Never fails a compaction: no network, and every path exits 0. It does ask the local
# client which instances are engaged — a local file read, no credential and no
# request — and falls through to the nudge if there is no client to ask.

set -u

. "$(dirname "$0")/_common.sh"

hooks_disabled && exit 0

# Same gate as the session-start hook: a project that never bound an instance
# hears nothing. Presence is all that is needed, so no JSON is parsed here.
bindings=$(find_binding_candidates)
[ -n "$bindings" ] || exit 0

# Presence is not enough: a binding whose entries are all `off` is deliberately
# dormant, and telling the model to write to a bound instance would undo that choice
# at exactly the moment it is least visible.
#
# Asked of the resolver rather than of the bytes. A grep over the raw files got this
# wrong three ways — see `has_tier` — including the default one, where `binding add
# <id>` writes no `tier` key at all and the nudge silently never appeared.
#
# When there is no client to ask, nudge anyway. The two failures are not equal: a
# missing nudge loses whatever the session learned, while a spare one costs a few
# lines the model is explicitly told it may skip. And the nudge points at the memory
# tools, which come over MCP and work whether or not a command-line client is
# installed, so silence there would be doubly wrong.
has_tier autoload available
case $? in
    0) : ;;
    2) : ;;
    *) exit 0 ;;
esac

# Plain text on stdout, not the `hookSpecificOutput` envelope the session-start hook
# uses. PreCompact does not accept that envelope: no output schema declares
# `hookEventName: "PreCompact"`, and the handler instead joins each hook's raw stdout
# into the custom instructions it carries into the compaction. So the envelope would
# have delivered our JSON to the model verbatim — or failed validation — while the
# nudge itself never arrived. This is the channel that event actually has.
printf '%s\n' "Context is about to be compacted, so anything learned in this session that is not
already written to xmemory will be lost.

Persist it now with the xmemory memory tools, if there is anything worth keeping:
decisions and the reasoning behind them, facts about people or projects, state
someone would need to resume this work. Write it to the bound instance it belongs
in — the one whose purpose covers it — and phrase it as durable fact rather than a
narration of the session.

Skip this silently when nothing durable came up, or when it is already saved. Do
not write a session summary for its own sake, and do not treat this as a prompt to
report back to the user."
