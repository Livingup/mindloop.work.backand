---
name: spread-ai-config-management
description: Manage Spread AI configuration through the local Runtime REST API. Use for Agent definitions, MCP service configuration, LLM model/provider/execution policy configuration, prompt snippets, and read-only Skill metadata. Do not use for runtime task lifecycle, tool execution, remote MCP consumption, logs, terminal, file resources, Scene, communication, or context-compression operations.
compatibility: Cross-platform. Requires Node.js 18+ available as node, or SPREAD_AI_CONFIG_MANAGEMENT_NODE pointing to a Node executable. Requires the Spread AI Runtime backend to be running and reachable on localhost.
metadata:
  display-name: Spread AI Configuration Management
  version: "1.2.1"
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

Normal self-management usage should authenticate through a UTF-8 login properties file supplied by the user. Ask the user for a properties file path and pass it with `--login-file <path>` or set `SPREAD_AI_RUNTIME_LOGIN_FILE`. The file must contain `username=...` and `password=...`; it may also contain `runtime_url=http://127.0.0.1:<port>`, `device_id=spread-ai-config-management`, and `client_kind=builtin-management-skill`. Do not ask for or pass username/password as command-line arguments.

The Node client calls `POST /api/auth/login`, uses the returned token only in memory as `Authorization: Bearer <token>` for the requested configuration REST call, and then makes a best-effort `POST /api/auth/logout` to revoke the script-owned session. It never prints properties content, passwords, or bearer tokens; never uses query-string tokens; and never reads PostgreSQL, SQLite, YAML, browser storage, or local Runtime configuration files to discover auth secrets.

Advanced diagnostic token sources remain available: explicit `--token-file`, `--token-stdin`, and `--auth-file` take precedence over `--login-file`; environment token/token-file/auth-file variables are fallback paths after login-file. If no auth source is provided, the script returns structured JSON with `code: "auth_required"`. Login failure and rate limiting also return structured JSON errors without credential values.

For writes, place UTF-8 JSON or supported YAML payloads in `--file <path>` or pass `--file -` to read the payload from stdin. JSON is the baseline cross-platform input format. The Node core owns the optional YAML parsing path; wrappers must not parse or transform complex payloads. Use `list` for summaries and `get --id <id>` for detail. For child-resource operations such as provider model bindings, execution-policy candidates, Agent MCP/Skill relations, or prompt content, pass the parent `--id` and a slash-separated `--sub-path` that maps to the REST child path, then use `create`, `update`, `patch`, `delete`, or `reorder` as appropriate.
