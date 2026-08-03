# shellcheck shell=sh
# Shared helpers for the xmemory hooks. Sourced, never executed.
#
# POSIX sh with no dependencies, on purpose. Claude Code ships as a native binary,
# so `node`, `python3` and `jq` are all absent on a plausible install; a hook that
# assumed any of them would fail on every session for those users. Everything here
# is shell builtins plus `sed`, `awk`, `tr`, `grep` and `dirname`, all of which POSIX
# guarantees.

BINDING_FILENAME=".xmemory.json"

# ── opt-out ────────────────────────────────────────────────────────────────
# Claude Code has no built-in way to switch off one hook of an installed plugin,
# so a plugin is expected to provide its own. Without this the only way to stop
# these hooks was to uninstall the plugin — losing its skills and MCP servers
# too — which is a poor answer for the case that needs it most: someone who
# already wired their own SessionStart or PreCompact hook and wants to keep it.
#
# Set XMEMORY_DISABLE_HOOKS to any non-empty value. The skills and MCP servers
# are unaffected; only these hooks stand down.
hooks_disabled() {
    [ -n "${XMEMORY_DISABLE_HOOKS:-}" ]
}

# ── where the session is rooted ────────────────────────────────────────────
# $CLAUDE_PROJECT_DIR, not the process's own working directory. A hook does not
# necessarily run with cwd set to the project: relying on `pwd` meant that from
# the wrong directory the walk below started in the wrong place, found no
# binding, and silently degraded a fully configured project to "nothing bound".
# Failing quietly in the direction of less context is exactly the bug that is
# hard to notice.
#
# The hook input JSON on stdin also carries a `cwd` field, which is what the
# environment variable falls back to elsewhere — but extracting one field from
# JSON in POSIX sh means a regex over arbitrary paths, and a wrong answer there
# is worse than the `pwd` fallback below.
# Resolved through `cd … && pwd -P` rather than used as given, so the walk starts
# from one stable spelling of the directory: a value carrying symlinks or a trailing
# slash would otherwise produce candidate paths that do not match what the CLI is
# later told through `--binding-dir`. The walk itself has no ceiling and compares
# nothing against $HOME — an earlier version did, and this comment outlived it. If
# the shell cannot resolve the value at all, the `-d` test fails and the working
# directory is used instead, so an unfamiliar path form costs nothing.
project_root() {
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR" ]; then
        (cd "$CLAUDE_PROJECT_DIR" 2>/dev/null && pwd -P) && return 0
    fi
    pwd -P 2>/dev/null
}

# ── could there be a binding in scope? ─────────────────────────────────────
# Deliberately a SUPERSET of what the CLI will accept, not a copy of its rule.
#
# The first version mirrored the CLI's ceiling ($HOME) and then the CLI's ceiling
# changed — it grew a git work-tree root outside a home directory — and this
# silently stopped finding bindings for every container, CI and /work checkout,
# exiting before it ever called the CLI. Encoding "presence" turned out to encode
# the ceiling rule too, so it drifted exactly like a second parser would have.
#
# That ceiling has since changed a second time, back to $HOME alone with no git at
# all. This file needed no edit for either change, which is the point: a superset
# does not track the rule, it only avoids paying for a CLI call that is certain to
# find nothing.
#
# So this walks to the filesystem root with no ceiling at all. It can only ever
# say "there might be something here" too eagerly, never too rarely, and the CLI
# then applies the real rule. One implementation of discovery, and this cannot
# disagree with it.
find_binding_candidates() {
    dir=$(project_root) || return 0
    [ -n "$dir" ] || return 0
    # Resolved the same way the walk spells its own directories. The walk uses
    # `pwd -P`, so comparing against a raw `$HOME` compared two different spellings of
    # one place: `HOME=/home/me/` or a `$HOME` reached through a symlink never set the
    # flag, the append below fired anyway, and the home binding was listed twice in
    # the install hint. Reproduced for both spellings; only the exactly-physical one
    # ever worked.
    home=$( [ -n "${HOME:-}" ] && cd "$HOME" 2>/dev/null && pwd -P )
    walked_home=0
    while :; do
        [ -n "$home" ] && [ "$dir" = "$home" ] && walked_home=1
        [ -f "$dir/$BINDING_FILENAME" ] && printf '%s\n' "$dir/$BINDING_FILENAME"
        parent=$(dirname "$dir")
        [ "$parent" = "$dir" ] && break
        dir=$parent
    done
    # Only when the walk did not already pass through it. For a project under $HOME —
    # the ordinary case — it did, and appending again listed the same path twice in
    # the install hint.
    if [ -n "$home" ] && [ -f "$home/$BINDING_FILENAME" ] && [ "$walked_home" != "1" ]; then
        printf '%s\n' "$home/$BINDING_FILENAME"
    fi
}

# ── emit ───────────────────────────────────────────────────────────────────
# The hook contract is one JSON document on stdout. Building it in sh means
# escaping the payload by hand: backslash first (or it would double-escape the
# quotes added after it), then quotes, then newlines to \n. Control characters
# other than the tab handled above are dropped rather than escaped — they have no
# business in injected context and would produce invalid JSON.
json_escape() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' \
        -e 's/[[:cntrl:]]//g' |
        awk 'BEGIN { ORS = "" } NR > 1 { print "\\n" } { print }'
}

# emit <event-name> <text> — writes the hook document and exits 0.
emit() {
    printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' \
        "$1" "$(printf '%s' "$2" | json_escape)"
    exit 0
}

# emit_with_message <event-name> <model-text> <user-text> — as `emit`, plus a
# top-level `systemMessage` shown to the user and not to the model.
#
# That split is the whole reason this exists. Anything the CLI says about a failure
# has repository-authored values interpolated into it — a committed instance name is
# free text — so it must not enter the model's context, but a person still needs to
# be told why their memory did not load. stderr looked like that channel and is not:
# the hook contract gives SessionStart "exit code 0 - stdout shown to Claude", and
# names stderr only for non-zero exits. A hook that succeeds, as this one deliberately
# always does, writes to a stream the user does not normally see — so the model was
# being told the user had been informed when they had not. `systemMessage` is the
# documented user channel for every hook, at any exit code.
emit_with_message() {
    printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' \
        "$(printf '%s' "$3" | json_escape)" "$1" "$(printf '%s' "$2" | json_escape)"
    exit 0
}

# ── what the binding actually resolves to ──────────────────────────────────
# The merged binding's tiers, one per line, or a non-zero return when there is no
# client installed to ask.
#
# Not a grep over the files. Whether the string `"autoload"` appears somewhere in a
# `.xmemory.json` answers a different question than the one being asked, and three
# ways: an entry with no `tier` key defaults to `available` and matched nothing —
# which is what `binding add <id>` writes by default, so the commonest binding of all
# read as dormant; a `tier: off` in `~/.xmemory.json` floors a project's `autoload`
# and still matched; and an entry whose *name* is "available" matched on its name.
#
# Scope precedence, the floor rule and the default tier all live in the CLI. Deriving
# them again here is the second parser this codebase keeps refusing to grow — the
# same reasoning that made `find_binding_candidates` a deliberate superset rather
# than a copy of the discovery rule.
#
# Two properties, both deliberate.
#
# It reads tier *fields* only. A name or purpose cannot forge one: the emitter escapes
# a quote inside a string value as \", so the unescaped `"tier":` sequence appears
# only where the emitter itself wrote it — an instance actually named `", "tier":
# "autoload` arrives with backslashes in front of those quotes and matches nothing.
#
# And it does not depend on the emitter's layout. Anchoring the field at the start of
# a line assumed today's two-space indentation, and would have gone quietly silent —
# no tiers found, so nothing engaged, so no nudge — if the JSON were ever emitted
# compactly. Splitting on commas first puts each field on a line of its own whatever
# the indentation, and a comma inside a string value can only split a value this
# never reads.
resolved_tiers() {
    command -v xmemcli >/dev/null 2>&1 || return 2
    # Captured before it is parsed, because a pipeline reports the status of its LAST
    # command. Parsing inline meant `sed` answered for the client: an unreadable or
    # malformed binding exits 2, the pipeline still exited 0 with no output, and the
    # third answer collapsed into the second — `has_tier` said "nothing is engaged"
    # where it should have said "I could not find out". Every caller reads those two
    # differently and on purpose, and the difference lands in the direction this
    # design avoids everywhere else: a compaction passing in silence with the
    # session's facts unwritten.
    _json=$(xmemcli --binding-dir "$(project_root)" --json binding list 2>/dev/null) || return 2
    printf '%s\n' "$_json" |
        tr ',' '\n' |
        sed -n 's/.*"tier"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p'
}

# has_tier <tier>… — 0 when the resolved binding carries any of the named tiers,
# 1 when it carries none, and 2 when there is no client to ask. Three answers and
# not two, because "I could not find out" is not the same as "no", and each caller
# should choose which way to fail rather than have silence chosen for it.
has_tier() {
    _tiers=$(resolved_tiers) || return 2
    for _want in "$@"; do
        printf '%s\n' "$_tiers" | grep -qx "$_want" && return 0
    done
    return 1
}
