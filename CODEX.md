# Codex compatibility

This repository is one plugin package with two client entry points:

- Claude Code reads `.claude-plugin/plugin.json`.
- Codex reads `.codex-plugin/plugin.json`.

Both clients load the same skills; neither manifest bundles an MCP entry, because connections are
registered per instance. They also share `hooks/hooks.json`: Codex
discovers that conventional path automatically when the plugin is enabled, so
`.codex-plugin/plugin.json` intentionally has no `hooks` entry. Installing the package in one
client does not create two entries in that client.

## Local setup

```text
codex plugin marketplace add xmemory-ai/claude-code-plugin
codex plugin add xmemory@xmemory-ai
```

Start a new session after installation. Codex lifecycle hooks require
`[features] hooks = true`; their current definitions must also be reviewed and
trusted with `/hooks`.

The `doctor` skill checks those two states separately. It also carries a manager
for the global `AGENTS.md` fallback:

```bash
sh skills/doctor/scripts/manage_codex_agents.sh status
sh skills/doctor/scripts/manage_codex_agents.sh install
sh skills/doctor/scripts/manage_codex_agents.sh remove
```

Those commands are run from the plugin root. The manager changes
only the text between `<!-- xmemory:managed:start -->` and
`<!-- xmemory:managed:end -->`. A non-empty global `AGENTS.override.md` shadows
`AGENTS.md`, which the manager reports.

## Per-instance connections

When `xmemcli` is installed and signed in, register a per-instance server through the local
transport so the credential is read from the CLI configuration on every connection. Name the
entry `xmemory-<id8>` — `xmemory-` plus the first eight characters of the instance id, the name
the instance's own setup instructions print — so two paths never register one instance twice:

```bash
xmemcli --json status
codex mcp add xmemory-<id8> -- xmemcli mcp <id>
```

Read both `version` and `authenticated` in the status output; its exit code does not establish
readiness. The `mcp` command requires `xmemcli` 0.0.7 or newer. Upgrade an older client with
`uv tool install --upgrade xmemcli`; if credentials are absent, run `xmemcli auth login` before
registering the server.

When the CLI is absent or cannot be upgraded, use the browser-authorized direct form instead:

```bash
codex mcp add xmemory-<id8> --url "https://mcp.xmemory.ai/instance/<id>"
codex mcp login xmemory-<id8>
```

The shared `connect` skill follows this same order for Claude Code and Codex while showing only
the command for the active client. The `doctor` skill inspects the entry first because a stdio
entry and a direct HTTP entry can surface the same authentication symptom but require different
fixes.

## Verification record

Verified on 2026-07-30 with Codex CLI 0.146.0 and revalidated against current main on
2026-08-04:

- The native Codex manifest passed the plugin validator.
- A local marketplace exposed and installed the native Codex package.
- Codex executed the installed SessionStart hook once and received its
  `additionalContext` through the Claude-compatible hook JSON. The stubbed
  `xmemcli` recorded exactly one invocation.
- Codex supplied `CLAUDE_PLUGIN_ROOT` to the shared hook command, matching its
  documented compatibility contract. No fork of the hook scripts was needed.
- A temporary Codex home loaded the installed managed block into the
  model-visible global `AGENTS.md` instructions.
- The Claude marketplace still passed `claude plugin validate --strict`.
- The hook suite passed 45 behavior checks; the managed-block suite passed 26.

The hook trust bypass was used only for the isolated automated execution after
the installed source had been validated. Normal setup uses `/hooks`.

## Surface findings

### ChatGPT desktop

Current OpenAI documentation makes plugins available in both ChatGPT Work and
Codex modes in the desktop app. Skills and MCP connections are shared plugin
components. Local command-hook execution is part of the Codex lifecycle-hook
runtime; it is not documented as a ChatGPT Work lifecycle facility.

The Codex CLI and desktop Codex mode share local configuration layers. The
installed hook was verified in the CLI runtime. A signed-in Work-mode UI check
is still required before claiming the shell hooks execute in Work mode.

### Import from Claude Code

The current flow is **Settings → Import** in the ChatGPT desktop app, not a
Codex `/import` slash command. The documented import inventory includes plugins,
hooks, MCP configuration, skills, instruction files, and settings. Imported
plugins or MCP connections may still require setup or authorization afterward.

The xmemory package should therefore be selected as one plugin during import;
its native Codex manifest remains the post-import package entry point. A
signed-in import UI run is still required before claiming this specific package
was migrated end to end.

### Codex cloud

The cloud environment documentation guarantees a repository checkout,
repository `AGENTS.md`, setup and maintenance scripts, environment variables,
and setup-only secrets. It does not establish that a local plugin marketplace,
local `config.toml`, plugin hooks, or local MCP authorization is copied into a
hosted cloud task.

The documented fallback is a setup script that installs `xmemcli`, plus
repository guidance that loads bound context manually:

```bash
uv tool install xmemcli
```

Cloud secrets are removed after the setup phase, while environment variables
remain during the agent phase. Do not place an account-wide xmemory key in a
cloud environment merely to make autoload work. This path needs an approved
scoped credential before it can be presented as a complete cloud setup.

## Official references

- [Build plugins](https://developers.openai.com/plugins/build/plugins)
- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [Plugins](https://learn.chatgpt.com/docs/plugins)
- [Import from another agent](https://learn.chatgpt.com/docs/import)
- [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environment)
