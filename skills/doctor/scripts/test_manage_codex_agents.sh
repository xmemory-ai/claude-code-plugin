#!/bin/sh
# Behavior tests for the reversible Codex AGENTS.md managed block.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P)
MANAGER=$SCRIPT_DIR/manage_codex_agents.sh
WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

PASS=0
FAIL=0

ok() {
    PASS=$((PASS + 1))
    printf '  ok    %s\n' "$1"
}

no() {
    FAIL=$((FAIL + 1))
    printf '  FAIL  %s\n' "$1"
    printf '        expected: %s\n' "$2"
    printf '        actual:   %s\n' "$3"
}

run_manager() {
    CODEX_HOME="$1" sh "$MANAGER" "$2"
}

assert_count() {
    _label=$1
    _needle=$2
    _file=$3
    _expected=$4
    _actual=$(awk -v needle="$_needle" '$0 == needle { count += 1 } END { print count + 0 }' "$_file")
    [ "$_actual" -eq "$_expected" ] && ok "$_label" ||
        no "$_label" "$_expected" "$_actual"
}

HOME_ONE="$WORK/codex home"

# ── 1. status distinguishes an absent fallback ───────────────────────────
out=$(run_manager "$HOME_ONE" status 2>&1)
status=$?
[ "$status" -eq 1 ] && ok "status reports an absent fallback" ||
    no "status reports an absent fallback" "exit 1" "exit $status: $out"

# ── 2. install creates the canonical block ───────────────────────────────
out=$(run_manager "$HOME_ONE" install 2>&1)
status=$?
[ "$status" -eq 0 ] && ok "install succeeds with a path containing spaces" ||
    no "install succeeds with a path containing spaces" "exit 0" "exit $status: $out"
AGENTS_ONE="$HOME_ONE/AGENTS.md"
assert_count "install writes one begin marker" '<!-- xmemory:managed:start -->' "$AGENTS_ONE" 1
assert_count "install writes one end marker" '<!-- xmemory:managed:end -->' "$AGENTS_ONE" 1
case "$(cat "$AGENTS_ONE")" in
    *'[features] hooks = true'*'/hooks'*'either change is reversible'*)
        ok "the block carries the factual hooks onboarding sentence"
        ;;
    *)
        no "the block carries the factual hooks onboarding sentence" \
            "feature flag, /hooks, and reversibility" "$(cat "$AGENTS_ONE")"
        ;;
esac
case "$(cat "$AGENTS_ONE")" in
    *'xmemcli context --text'*) ok "the block carries the manual context fallback" ;;
    *) no "the block carries the manual context fallback" "xmemcli context --text" "$(cat "$AGENTS_ONE")" ;;
esac

# ── 3. a second install is a byte-for-byte no-op ─────────────────────────
cp "$AGENTS_ONE" "$WORK/first-install"
out=$(run_manager "$HOME_ONE" install 2>&1)
status=$?
[ "$status" -eq 0 ] && cmp -s "$AGENTS_ONE" "$WORK/first-install" &&
    ok "install is idempotent" ||
    no "install is idempotent" "identical file after the second run" "exit $status: $out"

# ── 4. existing user guidance survives install and remove ────────────────
HOME_TWO="$WORK/preserved"
mkdir -p "$HOME_TWO"
printf '# My instructions\n\nKeep this line.\n' > "$HOME_TWO/AGENTS.md"
cp "$HOME_TWO/AGENTS.md" "$WORK/preserved-original"
chmod 640 "$HOME_TWO/AGENTS.md"
mode_before=$(stat -f '%Lp' "$HOME_TWO/AGENTS.md" 2>/dev/null || stat -c '%a' "$HOME_TWO/AGENTS.md")
inode_before=$(ls -di "$HOME_TWO/AGENTS.md" | awk '{ print $1 }')
run_manager "$HOME_TWO" install >/dev/null 2>&1
mode_after=$(stat -f '%Lp' "$HOME_TWO/AGENTS.md" 2>/dev/null || stat -c '%a' "$HOME_TWO/AGENTS.md")
inode_after=$(ls -di "$HOME_TWO/AGENTS.md" | awk '{ print $1 }')
[ "$mode_after" = "$mode_before" ] && ok "atomic install preserves the existing file mode" ||
    no "atomic install preserves the existing file mode" "$mode_before" "$mode_after"
[ "$inode_after" != "$inode_before" ] && ok "install replaces the file atomically" ||
    no "install replaces the file atomically" "a new inode" "$inode_after"
replacement_leaks=$(find "$HOME_TWO" -name '.AGENTS.md.xmemory.*' -print)
[ -z "$replacement_leaks" ] && ok "atomic install leaves no replacement file" ||
    no "atomic install leaves no replacement file" "no replacement file" "$replacement_leaks"
case "$(cat "$HOME_TWO/AGENTS.md")" in
    '# My instructions'*'Keep this line.'*) ok "install preserves existing guidance" ;;
    *) no "install preserves existing guidance" "the original two lines" "$(cat "$HOME_TWO/AGENTS.md")" ;;
esac
out=$(run_manager "$HOME_TWO" remove 2>&1)
status=$?
[ "$status" -eq 0 ] && cmp -s "$HOME_TWO/AGENTS.md" "$WORK/preserved-original" &&
    ok "install and remove restore existing guidance byte for byte" ||
    no "install and remove restore existing guidance byte for byte" \
        "the exact original file" "exit $status: $out; content: $(cat "$HOME_TWO/AGENTS.md")"

cycle=1
while [ "$cycle" -le 5 ]; do
    run_manager "$HOME_TWO" install >/dev/null 2>&1 || break
    run_manager "$HOME_TWO" remove >/dev/null 2>&1 || break
    cycle=$((cycle + 1))
done
[ "$cycle" -eq 6 ] && cmp -s "$HOME_TWO/AGENTS.md" "$WORK/preserved-original" &&
    ok "repeated install and remove cycles do not accumulate whitespace" ||
    no "repeated install and remove cycles do not accumulate whitespace" \
        "the exact original file after five cycles" "stopped at cycle $cycle; content: $(cat "$HOME_TWO/AGENTS.md")"

HOME_TRAILING_BLANKS="$WORK/trailing blanks"
mkdir -p "$HOME_TRAILING_BLANKS"
printf 'Keep these blanks.\n\n\n' > "$HOME_TRAILING_BLANKS/AGENTS.md"
cp "$HOME_TRAILING_BLANKS/AGENTS.md" "$WORK/trailing-blanks-original"
run_manager "$HOME_TRAILING_BLANKS" install >/dev/null 2>&1
run_manager "$HOME_TRAILING_BLANKS" remove >/dev/null 2>&1
cmp -s "$HOME_TRAILING_BLANKS/AGENTS.md" "$WORK/trailing-blanks-original" &&
    ok "round trip preserves user-owned trailing blank lines" ||
    no "round trip preserves user-owned trailing blank lines" \
        "the exact original file" "$(cat "$HOME_TRAILING_BLANKS/AGENTS.md")"

HOME_LEGACY="$WORK/legacy separator"
mkdir -p "$HOME_LEGACY"
printf 'Legacy guidance.\n' > "$WORK/legacy-original"
cp "$WORK/legacy-original" "$HOME_LEGACY/AGENTS.md"
cat >> "$HOME_LEGACY/AGENTS.md" <<'EOF'

<!-- xmemory:managed:start -->
old managed text
<!-- xmemory:managed:end -->
EOF
run_manager "$HOME_LEGACY" remove >/dev/null 2>&1
cmp -s "$HOME_LEGACY/AGENTS.md" "$WORK/legacy-original" &&
    ok "remove cleans up the separator written by the previous layout" ||
    no "remove cleans up the separator written by the previous layout" \
        "the file before the legacy install" "$(cat "$HOME_LEGACY/AGENTS.md")"

# ── 5. remove is idempotent ──────────────────────────────────────────────
cp "$HOME_TWO/AGENTS.md" "$WORK/first-remove"
out=$(run_manager "$HOME_TWO" remove 2>&1)
status=$?
[ "$status" -eq 0 ] && cmp -s "$HOME_TWO/AGENTS.md" "$WORK/first-remove" &&
    ok "remove is idempotent" ||
    no "remove is idempotent" "identical file after the second run" "exit $status: $out"

# ── 6. an older complete block is replaced, not duplicated ───────────────
HOME_THREE="$WORK/update"
mkdir -p "$HOME_THREE"
cat > "$HOME_THREE/AGENTS.md" <<'EOF'
Before.
<!-- xmemory:managed:start -->
old managed text
<!-- xmemory:managed:end -->
After.
EOF
out=$(run_manager "$HOME_THREE" status 2>&1)
status=$?
case "$out" in
    *'needs refresh'*) [ "$status" -eq 1 ] && ok "status detects a stale managed block" ||
        no "status detects a stale managed block" "exit 1" "exit $status: $out" ;;
    *) no "status detects a stale managed block" "needs refresh" "$out" ;;
esac
run_manager "$HOME_THREE" install >/dev/null 2>&1
assert_count "update keeps one begin marker" '<!-- xmemory:managed:start -->' "$HOME_THREE/AGENTS.md" 1
case "$(cat "$HOME_THREE/AGENTS.md")" in
    *'old managed text'*)
        no "update replaces stale managed text" "old text absent" "$(cat "$HOME_THREE/AGENTS.md")"
        ;;
    *'Before.'*'After.'*) ok "update replaces stale managed text" ;;
    *) no "update replaces stale managed text" "surrounding text preserved" "$(cat "$HOME_THREE/AGENTS.md")" ;;
esac

# ── 7. malformed markers fail closed and preserve the file ───────────────
HOME_FOUR="$WORK/malformed"
mkdir -p "$HOME_FOUR"
printf 'Before.\n<!-- xmemory:managed:start -->\nunfinished\n' > "$HOME_FOUR/AGENTS.md"
cp "$HOME_FOUR/AGENTS.md" "$WORK/malformed-before"
out=$(run_manager "$HOME_FOUR" install 2>&1)
status=$?
[ "$status" -eq 2 ] && cmp -s "$HOME_FOUR/AGENTS.md" "$WORK/malformed-before" &&
    ok "malformed markers fail closed" ||
    no "malformed markers fail closed" "exit 2 and unchanged file" "exit $status: $out"

HOME_REVERSED="$WORK/reversed"
mkdir -p "$HOME_REVERSED"
cat > "$HOME_REVERSED/AGENTS.md" <<'EOF'
Before.
<!-- xmemory:managed:end -->
middle
<!-- xmemory:managed:start -->
After.
EOF
cp "$HOME_REVERSED/AGENTS.md" "$WORK/reversed-before"
out=$(run_manager "$HOME_REVERSED" install 2>&1)
status=$?
[ "$status" -eq 2 ] && cmp -s "$HOME_REVERSED/AGENTS.md" "$WORK/reversed-before" &&
    ok "reversed markers fail closed" ||
    no "reversed markers fail closed" "exit 2 and unchanged file" "exit $status: $out"

# ── 8. a non-empty global override is reported as shadowing ──────────────
HOME_FIVE="$WORK/override"
mkdir -p "$HOME_FIVE"
printf '# Temporary override\n' > "$HOME_FIVE/AGENTS.override.md"
out=$(run_manager "$HOME_FIVE" install 2>&1)
status=$?
case "$out" in
    *'loads it instead'*) [ "$status" -eq 0 ] && ok "a shadowing override is reported" ||
        no "a shadowing override is reported" "exit 0" "exit $status: $out" ;;
    *) no "a shadowing override is reported" "loads it instead" "$out" ;;
esac

# ── 9. HOME fallback and missing-home refusal are explicit ───────────────
HOME_SIX="$WORK/home fallback"
out=$(HOME="$HOME_SIX" CODEX_HOME= sh "$MANAGER" install 2>&1)
status=$?
[ "$status" -eq 0 ] && [ -f "$HOME_SIX/.codex/AGENTS.md" ] &&
    ok "HOME resolves the default Codex home" ||
    no "HOME resolves the default Codex home" "a fallback under HOME/.codex" "exit $status: $out"
out=$(HOME= CODEX_HOME= sh "$MANAGER" install 2>&1)
status=$?
[ "$status" -eq 2 ] && ok "missing CODEX_HOME and HOME is refused" ||
    no "missing CODEX_HOME and HOME is refused" "exit 2" "exit $status: $out"

# ── 10. a dangling AGENTS.md symlink is never followed ───────────────────
HOME_SEVEN="$WORK/dangling"
mkdir -p "$HOME_SEVEN"
ln -s "$WORK/does-not-exist" "$HOME_SEVEN/AGENTS.md"
out=$(run_manager "$HOME_SEVEN" install 2>&1)
status=$?
[ "$status" -eq 2 ] && [ ! -e "$WORK/does-not-exist" ] &&
    ok "a dangling AGENTS.md symlink is refused" ||
    no "a dangling AGENTS.md symlink is refused" \
        "exit 2 and no linked target" "exit $status: $out"

HOME_EIGHT="$WORK/live symlink"
mkdir -p "$HOME_EIGHT"
printf 'linked guidance\n' > "$WORK/linked-agents"
ln -s "$WORK/linked-agents" "$HOME_EIGHT/AGENTS.md"
out=$(run_manager "$HOME_EIGHT" install 2>&1)
status=$?
[ "$status" -eq 2 ] && [ "$(cat "$WORK/linked-agents")" = "linked guidance" ] &&
    ok "a live AGENTS.md symlink is refused" ||
    no "a live AGENTS.md symlink is refused" \
        "exit 2 and unchanged linked target" "exit $status: $out"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
