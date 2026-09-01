#!/bin/sh
# Shape tests for the plugin package itself.
#
# The hooks have behaviour tests in hooks/test_hooks.sh; this covers the promises the
# manifests and the documentation make about the package. The one that matters most is
# that the plugin bundles no MCP entry: a bundled entry is registered for everyone who
# installs the plugin, cannot be bound to an instance, and sits in the client's MCP list
# as "needs authentication" for anyone who connects per instance instead. Removing it
# once is easy; keeping it removed needs a check, because every manifest and doc here
# once described it as the way in.
#
#   sh test_manifest.sh
#
# Exit 0 and a passing count means the package still has the shape the README describes.
set -u

ROOT=$(cd "$(dirname "$0")" && pwd -P)
cd "$ROOT" || exit 1

PASS=0
FAIL=0

ok() {
    PASS=$((PASS + 1))
    printf '  ok    %s\n' "$1"
}

no() {
    FAIL=$((FAIL + 1))
    printf '  FAIL  %s\n' "$1"
    printf '        %s\n' "$2"
}

# --- no bundled MCP entry ---------------------------------------------------------

if [ -e .mcp.json ]; then
    no "no .mcp.json at the plugin root" "a root .mcp.json is registered for every installer"
else
    ok "no .mcp.json at the plugin root"
fi

for manifest in .claude-plugin/plugin.json .codex-plugin/plugin.json; do
    if grep -q '"mcpServers"' "$manifest"; then
        no "$manifest declares no mcpServers" "found an mcpServers key"
    else
        ok "$manifest declares no mcpServers"
    fi
done

# --- the docs describe the package that ships ----------------------------------------

# Phrases that only made sense while the plugin registered its own entries. A doc that
# still carries one is describing a package that no longer exists.
DOCS="README.md CODEX.md MIGRATION.md skills/connect/SKILL.md skills/doctor/SKILL.md skills/xmemory-memory/SKILL.md skills/VERIFYING.md"
for phrase in \
    'registers two MCP servers' \
    'bundled `xmemory` server' \
    'bundled `xmemory` entry' \
    'reuse the same `.mcp.json`' \
    '## Two connections'; do
    hit=$(grep -l -F -- "$phrase" $DOCS 2>/dev/null)
    if [ -n "$hit" ]; then
        no "docs do not say: $phrase" "in: $(printf '%s' "$hit" | tr '\n' ' ')"
    else
        ok "docs do not say: $phrase"
    fi
done

# The skills the README's What-ships table lists exist on disk under the names it gives them.
for skill in connect doctor xmemory-memory; do
    if [ -f "skills/$skill/SKILL.md" ] && grep -q "^name: $skill\$" "skills/$skill/SKILL.md"; then
        ok "skills/$skill/SKILL.md exists and is named $skill"
    else
        no "skills/$skill/SKILL.md exists and is named $skill" "missing, or its frontmatter names something else"
    fi
done

# --- one version, spelled once per manifest --------------------------------------------

version_of() {
    sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1
}
claude_v=$(version_of .claude-plugin/plugin.json)
codex_v=$(version_of .codex-plugin/plugin.json)
market_v=$(version_of .claude-plugin/marketplace.json)
if [ -n "$claude_v" ] && [ "$claude_v" = "$codex_v" ] && [ "$claude_v" = "$market_v" ]; then
    ok "manifests agree on version $claude_v"
else
    no "manifests agree on one version" "claude=$claude_v codex=$codex_v marketplace=$market_v"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
