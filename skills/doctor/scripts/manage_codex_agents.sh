#!/bin/sh
# Install, inspect, or remove xmemory's managed block in Codex's global AGENTS.md.
#
# The block is deliberately small. Codex loads global AGENTS.md before every
# session even when lifecycle hooks are disabled or have not yet been trusted.
# This script never edits content outside its two marker lines. It replaces an
# ordinary AGENTS.md atomically and refuses symbolic links rather than risking
# an in-place partial write to their target.

set -u

BEGIN_MARKER='<!-- xmemory:managed:start -->'
END_MARKER='<!-- xmemory:managed:end -->'

usage() {
    printf 'Usage: %s install|status|remove\n' "$0" >&2
    exit 2
}

managed_block() {
    cat <<'EOF'
<!-- xmemory:managed:start -->

## xmemory

When a `.xmemory.json` binding is in scope and no xmemory context was supplied for this session,
use the bundled `doctor` skill. If `xmemcli` is available, `xmemcli context --text` loads the
bound `autoload` context manually.

If doctor finds Codex hooks disabled or awaiting trust, tell the user in one sentence: "Codex
hooks load bound xmemory context at session lifecycle points; enable them with
`[features] hooks = true` and review them with `/hooks`, and either change is reversible."

When relying on a recalled xmemory record, cite it by name and include its link when the tool
returns one.
<!-- xmemory:managed:end -->
EOF
}

marker_count() {
    awk -v marker="$1" '$0 == marker { count += 1 } END { print count + 0 }' "$2"
}

validate_markers() {
    _begin_count=$(marker_count "$BEGIN_MARKER" "$TARGET")
    _end_count=$(marker_count "$END_MARKER" "$TARGET")
    _markers_are_ordered=1
    if [ "$_begin_count" -eq 1 ] && ! awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { if (state != 0) invalid = 1; state = 1; next }
        $0 == end { if (state != 1) invalid = 1; state = 2; next }
        END { if (state != 2) invalid = 1; exit invalid }
    ' "$TARGET"; then
        _markers_are_ordered=0
    fi
    if [ "$_begin_count" -ne "$_end_count" ] || [ "$_begin_count" -gt 1 ] || \
        [ "$_markers_are_ordered" -ne 1 ]; then
        printf 'xmemory: refusing to edit %s: managed block markers are incomplete, duplicated, or out of order\n' \
            "$TARGET" >&2
        exit 2
    fi
}

strip_managed_block() {
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        !managed && $0 == begin {
            managed = 1
            deciding_layout = 1
            saved_line = pending_line
            saved_line_exists = pending_line_exists
            pending_line_exists = 0
            next
        }
        managed {
            if (deciding_layout) {
                # The current layout starts with a blank line inside the
                # markers. Preserve any preceding user-owned blank line.
                # Older releases put their separator before the begin marker;
                # remove that one legacy separator during migration.
                if (saved_line_exists && ($0 == "" || saved_line != "")) {
                    print saved_line
                }
                deciding_layout = 0
            }
            if ($0 == end) {
                managed = 0
            }
            next
        }
        {
            if (pending_line_exists) {
                print pending_line
            }
            pending_line = $0
            pending_line_exists = 1
        }
        END {
            if (!managed && pending_line_exists) {
                print pending_line
            }
        }
    ' "$TARGET"
}

current_managed_block() {
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { managed = 1 }
        managed { print }
        $0 == end { managed = 0 }
    ' "$TARGET"
}

warn_if_shadowed() {
    _override="$CODEX_HOME_DIR/AGENTS.override.md"
    if [ -s "$_override" ]; then
        printf 'xmemory: %s is non-empty, so Codex loads it instead of %s\n' \
            "$_override" "$TARGET" >&2
    fi
}

ACTION=${1:-}
[ "$#" -eq 1 ] || usage
case "$ACTION" in
    install|status|remove) ;;
    *) usage ;;
esac

if [ -n "${CODEX_HOME:-}" ]; then
    CODEX_HOME_DIR=$CODEX_HOME
else
    [ -n "${HOME:-}" ] && [ "$HOME" != "/" ] || {
        printf 'xmemory: CODEX_HOME is unset and HOME does not name a user directory\n' >&2
        exit 2
    }
    CODEX_HOME_DIR=$HOME/.codex
fi
TARGET=$CODEX_HOME_DIR/AGENTS.md

if [ "$ACTION" = install ]; then
    mkdir -p "$CODEX_HOME_DIR" || exit 1
fi

if [ -L "$TARGET" ]; then
    printf 'xmemory: refusing to edit %s because it is a symbolic link\n' \
        "$TARGET" >&2
    exit 2
fi

if [ ! -e "$TARGET" ]; then
    case "$ACTION" in
        status)
            printf 'xmemory AGENTS.md fallback is not installed at %s\n' "$TARGET"
            exit 1
            ;;
        remove)
            printf 'xmemory AGENTS.md fallback is already absent from %s\n' "$TARGET"
            exit 0
            ;;
        install) ;;
    esac
    TARGET_EXISTS=0
    BEGIN_COUNT=0
else
    [ -f "$TARGET" ] || {
        printf 'xmemory: refusing to edit %s because it is not a regular file\n' "$TARGET" >&2
        exit 2
    }
    validate_markers
    TARGET_EXISTS=1
    BEGIN_COUNT=$(marker_count "$BEGIN_MARKER" "$TARGET")
fi

if [ "$ACTION" = status ]; then
    if [ "$BEGIN_COUNT" -eq 1 ]; then
        CURRENT_BLOCK=$(current_managed_block)
        CANONICAL_BLOCK=$(managed_block)
        if [ "$CURRENT_BLOCK" = "$CANONICAL_BLOCK" ]; then
            printf 'xmemory AGENTS.md fallback is installed at %s\n' "$TARGET"
            warn_if_shadowed
            exit 0
        fi
        printf 'xmemory AGENTS.md fallback at %s needs refresh; run install\n' "$TARGET"
        exit 1
    fi
    printf 'xmemory AGENTS.md fallback is not installed at %s\n' "$TARGET"
    exit 1
fi

if [ "$ACTION" = remove ] && [ "$BEGIN_COUNT" -eq 0 ]; then
    printf 'xmemory AGENTS.md fallback is already absent from %s\n' "$TARGET"
    exit 0
fi

WORK_FILE=
BLOCK_FILE=
REPLACEMENT_FILE=
cleanup_files() {
    [ -z "$WORK_FILE" ] || rm -f "$WORK_FILE"
    [ -z "$BLOCK_FILE" ] || rm -f "$BLOCK_FILE"
    [ -z "$REPLACEMENT_FILE" ] || rm -f "$REPLACEMENT_FILE"
}
trap cleanup_files EXIT HUP INT TERM

WORK_FILE=$(mktemp "${TMPDIR:-/tmp}/xmemory-agents.XXXXXX") || exit 1
BLOCK_FILE=$(mktemp "${TMPDIR:-/tmp}/xmemory-agents-block.XXXXXX") || exit 1

atomic_replace_target() {
    umask 077
    REPLACEMENT_FILE=$(mktemp "$CODEX_HOME_DIR/.AGENTS.md.xmemory.XXXXXX") || return 1
    if [ "$TARGET_EXISTS" -eq 1 ]; then
        cp -p "$TARGET" "$REPLACEMENT_FILE" || return 1
    else
        chmod 600 "$REPLACEMENT_FILE" || return 1
    fi
    cat "$1" > "$REPLACEMENT_FILE" || return 1
    mv -f "$REPLACEMENT_FILE" "$TARGET" || return 1
    REPLACEMENT_FILE=
}

if [ "$TARGET_EXISTS" -eq 1 ]; then
    strip_managed_block > "$WORK_FILE" || exit 1
else
    : > "$WORK_FILE" || exit 1
fi

if [ "$ACTION" = install ]; then
    managed_block > "$BLOCK_FILE" || exit 1
    if [ "$BEGIN_COUNT" -eq 1 ]; then
        CURRENT_BLOCK=$(current_managed_block)
        CANONICAL_BLOCK=$(managed_block)
        if [ "$CURRENT_BLOCK" = "$CANONICAL_BLOCK" ]; then
            printf 'xmemory AGENTS.md fallback is already current at %s\n' "$TARGET"
            warn_if_shadowed
            exit 0
        fi
    fi

    cat "$BLOCK_FILE" >> "$WORK_FILE" || exit 1
    atomic_replace_target "$WORK_FILE" || exit 1
    printf 'Installed xmemory AGENTS.md fallback at %s\n' "$TARGET"
    warn_if_shadowed
    exit 0
fi

atomic_replace_target "$WORK_FILE" || exit 1
printf 'Removed xmemory AGENTS.md fallback from %s\n' "$TARGET"
