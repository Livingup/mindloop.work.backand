---
name: spread-ai-config-management
description: Manage Spread AI configuration through the local Runtime REST API. Use for Agent definitions, MCP service configuration, LLM model/provider/execution policy configuration, prompt snippets, and read-only Skill metadata. Do not use for runtime task lifecycle, tool execution, remote MCP consumption, logs, terminal, file resources, Scene, communication, or context-compression operations.
compatibility: Cross-platform. Requires Node.js 18+ available as node, or SPREAD_AI_CONFIG_MANAGEMENT_NODE pointing to a Node executable. Requires the Spread AI Runtime backend to be running and reachable on localhost.
metadata:
  display-name: Spread AI Configuration Management
  version: "1.2.0"
  managed-by: spread-ai-runtime
  entrypoint.node: scripts/manage-config.mjs
  entrypoint.windows-cmd: scripts/manage-config.cmd
  entrypoint.windows-powershell: scripts/manage-config.ps1
  entrypoint.posix-shell: scripts/manage-config
allowed-tools: terminal_execute_command terminal_read_output
---

# Spread AI Configuration Management

Use this built-in Skill when a user asks to manage persistent Spread AI configuration for:

- Agent definition metadata and references.
- MCP service configuration.
- LLM model metadata, provider definitions, and execution policies.
- Prompt snippets/templates.
- Skill metadata lookup only.

# Operating boundary

This Skill is a REST client for the local Runtime backend. It must not write PostgreSQL, YAML, SQLite, or local configuration files directly. Start or locate the Runtime backend first, then call its authenticated REST API.

Do not use this Skill for runtime task lifecycle, tool execution, remote MCP service calls, log streaming, terminal sessions, file resources, Scene/session management, communication streams, or context-compression runtime operations.

# Cross-platform entrypoints

The single core implementation is:

```text
scripts/manage-config.mjs <resource> <action> [options]
```

Run it with Node.js 18+ on Windows, macOS, or Linux. The package also includes thin wrappers that delegate to the same Node implementation:

- Windows CMD: `scripts/manage-config.cmd`
- Windows PowerShell: `scripts/manage-config.ps1`
- macOS/Linux POSIX shell: `scripts/manage-config`

The built-in Skill is installed as one cross-platform package on every platform. Platform differences are represented by entrypoint metadata and wrappers inside the package; the installer must not copy different Skill directories per platform.

# Authentication and payloads

Authentication secrets must be supplied through `--token-file`, `--token-stdin`, `--auth-file`, or environment variables. Do not place passwords, API keys, or bearer tokens directly in command-line arguments.

For writes, place UTF-8 JSON or supported YAML payloads in `--file <path>` or pass `--file -` to read the payload from stdin. JSON is the baseline cross-platform input format. The Node core owns the optional YAML parsing path; wrappers must not parse or transform complex payloads. Use `list` for summaries and `get --id <id>` for detail. For child-resource operations such as provider model bindings, execution-policy candidates, Agent MCP/Skill relations, or prompt content, pass the parent `--id` and a slash-separated `--sub-path` that maps to the REST child path, then use `create`, `update`, `patch`, `delete`, or `reorder` as appropriate.
