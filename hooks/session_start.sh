#!/bin/sh
# xmemory SessionStart hook — inject the context for instances bound "autoload" here.
#
# Three tiers, and the first two never touch the network:
#
#   no .xmemory.json      -> silent, exit 0. A project that never bound anything must
#                            not learn this plugin exists.
#   binding, no xmemcli   -> one short line naming what is bound and how to enable
#                            preloading. The MCP connection still carries the
#                            instance's own instructions, so this is an upgrade
#                            prompt, not a failure.
#   binding + xmemcli     -> the rendered pack, injected verbatim.
#
# It must never fail a session start. Every path exits 0; a broken binding, an
# expired credential or an unreachable API degrades to less context, never to a
# blocked session.

set -u

# Well below the harness's own hook timeout, so the CLI gives up on its own terms
# and injects nothing, rather than being killed mid-request and reported as a
# failed hook. A session start should never wait long for optional context.
FETCH_TIMEOUT_SECONDS=12

. "$(dirname "$0")/_common.sh"

hooks_disabled && exit 0

bindings=$(find_binding_candidates)
[ -n "$bindings" ] || exit 0

if ! command -v xmemcli >/dev/null 2>&1; then
    # Only worth saying when something is actually tiered `autoload`. A binding
    # that is entirely `available` preloads nothing by design, so the CLI would
    # change nothing for it — and without this check every such project got an
    # install prompt on every session, forever, for a feature it had declined.
    #
    # A grep, not a parse, and deliberately so — this is the one branch where the
    # authoritative resolver cannot be asked, because the whole condition is that
    # there is no client to ask it. The stakes are only whether a hint appears, so
    # this does not reopen the rule that all real parsing lives in the CLI.
    #
    # But it matches the **field**, not the word. Searching for `"autoload"` anywhere
    # in the file hit any string equal to it: an `available` binding whose name is
    # "autoload", or whose engage cue is, produced the install prompt on every session
    # forever — for a feature that project had declined, which is the exact outcome
    # this check exists to prevent. Newlines are folded first so a key and its value
    # split across lines still match, and an escaped quote inside a name cannot forge
    # the pair, because JSON requires a `"` inside a string to be written `\"`.
    #
    # Driven from a here-doc into a plain flag, not a pipeline into `while`. The
    # pipeline version signalled with `exit 7` and relied on the loop running in a
    # subshell — which POSIX does not require, and which bash's `lastpipe` disables,
    # so there the `exit` would have taken the whole hook down with status 7 and
    # broken this file's every-path-exits-0 rule.
    autoload=0
    while IFS= read -r file; do
        [ -n "$file" ] || continue
        if tr '\n' ' ' < "$file" 2>/dev/null |
            grep -qE '"tier"[[:space:]]*:[[:space:]]*"autoload"' 2>/dev/null; then
            autoload=1
            break
        fi
    done <<EOF
$bindings
EOF
    [ "$autoload" = "1" ] || exit 0
    emit "SessionStart" "This project binds an xmemory instance for autoload ($(printf '%s' "$bindings" | tr '\n' ' ')), but the xmemory CLI is not installed, so it was not preloaded.

Memory tools still work over the xmemory MCP connection if one is authorised. To preload bound instances at session start, the user can install the CLI: 'uv tool install xmemcli' then 'xmemcli auth login' (or 'xmemcli auth login --email <address>' for a headless emailed approval). Mention this once if it is relevant; do not repeat it."
fi

# --binding-dir pins the CLI to the same root the walk above used. Without it the
# CLI would resolve the binding from *its* working directory, so the two halves
# could disagree about which project this is — the hook reporting a binding the
# fetch then failed to find.
#
# --text prints the pack verbatim; without it a pipe would yield a JSON document,
# which is not what belongs in a session.
#
# Deliberately NOT gated on `has_tier autoload`, though the no-CLI branch above makes
# exactly that test. The asymmetry is real and it is the cheaper side: `context`
# already returns empty without contacting anything when nothing is tiered autoload,
# so gating would spend one local client call (`binding list`) to avoid another
# (`context`) and buy a branch. Up there the test earns its place because there is no
# client to call and the alternative is prompting someone forever about a feature
# they declined.
errors=$(mktemp 2>/dev/null) || errors=""
if [ -n "$errors" ]; then
    pack=$(xmemcli --binding-dir "$(project_root)" context --timeout "$FETCH_TIMEOUT_SECONDS" --text 2>"$errors")
    status=$?
    # The LAST non-empty line, not the first. The CLI writes warnings while it
    # works and emits the failure last, so `head` reported a preceding warning as
    # though it were the cause — a session start that failed on a missing
    # credential blamed the provenance notice instead.
    reason=$(awk 'NF { last = $0 } END { print last }' "$errors" 2>/dev/null)
    rm -f "$errors"
else
    pack=$(xmemcli --binding-dir "$(project_root)" context --timeout "$FETCH_TIMEOUT_SECONDS" --text 2>/dev/null)
    status=$?
    # No temp file, so the CLI's own explanation is unavailable. A fixed reason on its
    # own is not the answer either: it fired the branch below for a *correct* empty
    # result — an `available`/`off`-only binding exits 0 with an empty pack and an
    # empty stderr — and reported that context could not be loaded on every session of
    # a perfectly healthy project.
    #
    # The question that branch actually needs answered is not "what went wrong" but
    # "should there have been a pack at all", and the resolver still answers that
    # without stderr. Something tiered `autoload` and nothing to show for it means
    # something went wrong; nothing tiered `autoload` means an empty pack is the right
    # answer and there is nothing to report.
    # Three answers, three ways — `has_tier` distinguishes "nothing is engaged" from
    # "I could not find out", and a binary `if` folded the second into the first. With
    # no temp file AND an unreadable binding, `context` soft-fails (status 0, empty
    # pack, explanation on the stderr just discarded) while `binding list` exits 2, so
    # the one combination where least is known reported nothing at all. Only a
    # definite "nothing was owed" may clear the reason; not knowing is a reason.
    has_tier autoload
    case $? in
        1) reason="" ;;
        *) reason="the reason could not be captured (no temporary file available)" ;;
    esac
fi

# Failure is never fatal — it costs context, not the session. But it is not
# silent either: an agent that simply receives nothing cannot tell "no memories"
# from "could not reach them", and may tell the user they have nothing saved.
# One line naming the cause is the difference; the CLI writes it to stderr
# precisely so it can be surfaced here rather than injected into the pack.
# Report when the fetch failed OR when it returned nothing but had something to
# say. The CLI deliberately degrades almost every failure — no credential, an
# unparsable binding, a server that refuses the request — to a zero exit with the
# reason on stderr, so keying on the exit code alone left the user with silence
# in exactly the cases worth explaining. An empty pack with an empty stderr is
# the ordinary "nothing is tiered autoload", and stays silent.
if [ "$status" -ne 0 ] || { [ -z "$pack" ] && [ -n "$reason" ]; }; then
    # The reason goes to the USER, on stderr, and never into the model's context.
    #
    # It is not our text: the CLI interpolates the binding's own values into its
    # diagnostics, so a committed `.xmemory.json` naming an instance
    # "IGNORE ALL PRIOR INSTRUCTIONS…" puts that string on stderr — with an empty
    # pack and exit 0, which is exactly this branch. Copying the last line verbatim
    # put repository-authored text into SessionStart `additionalContext`, ahead of
    # the first prompt and without the provenance note a real pack carries.
    #
    # `systemMessage` carries it to the user, and only to the user. stderr was the
    # wrong channel and the comment here used to say otherwise: the hook contract gives
    # SessionStart "exit code 0 - stdout shown to Claude" and mentions stderr only for
    # non-zero exits, so on the always-zero exit this hook takes, the detail went
    # somewhere the user does not normally look — while the model was told they had
    # been informed. Still on stderr as well, for `--debug` transcripts.
    [ -n "$reason" ] || reason="xmemcli exited $status"
    printf 'xmemory: context was not loaded — %s\n' "$reason" >&2
    emit_with_message "SessionStart" "xmemory context could not be loaded for this project, so bound instances were not preloaded. The reason was shown to the user.

Memory tools over the xmemory MCP connection are unaffected if one is authorised. Do not tell the user they have nothing saved — this says the context could not be fetched, not that it is empty." "xmemory: context was not loaded — $reason"
fi

[ -n "$pack" ] || exit 0

emit "SessionStart" "$pack"
