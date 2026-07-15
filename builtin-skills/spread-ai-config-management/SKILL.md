---
name: spread-ai-config-management
description: Manage Spread AI configuration through the local Runtime REST API. Use for Agent definitions, MCP service configuration, LLM model/provider/execution policy configuration, prompt snippets, read-only Skill metadata, and read-only local tool metadata discovery for Agent tool_ids. Do not use for runtime task lifecycle, tool execution, remote MCP consumption, logs, terminal, file resources, Scene, communication, or context-compression operations.
compatibility: Cross-platform through Node.js 18+. Runtime/Agent callers invoke the Node entrypoint directly; this Skill does not rely on OS-specific wrapper scripts.
metadata:
  display-name: Spread AI Configuration Management
  version: "1.2.3"
  managed-by: spread-ai-runtime
  entrypoint.node: scripts/manage-config.mjs
allowed-tools: terminal_execute_command terminal_read_output
---

# Spread AI Configuration Management

Use this built-in Skill when a user asks to manage persistent Spread AI configuration for:

- Agent definition metadata and references.
- MCP service configuration.
- LLM model metadata, provider definitions, and execution policies.
- Prompt snippets/templates.
- Skill metadata lookup only.
- Local Runtime tool metadata lookup only, to discover valid tool IDs/details for Agent definition `tool_ids`.

# Operating boundary

This Skill is a REST client for the local Runtime backend. It must not write PostgreSQL, YAML, SQLite, or local configuration files directly. Start or locate the Runtime backend first, then call its authenticated REST API.

Do not use this Skill for runtime task lifecycle, tool execution, remote MCP service calls, log streaming, terminal sessions, file resources, Scene/session management, communication streams, or context-compression runtime operations. The `tool` resource is intentionally limited to read-only local metadata discovery (`list` and `get`) so Agent definitions can reference valid `tool_ids`; it never calls `/api/tools/{name}/exec`.

# Cross-platform entrypoint

The formal Runtime/Agent entrypoint is the Node.js script:

```text
node scripts/manage-config.mjs <resource> <action> [options]
```

Node.js 18+ is the cross-platform runtime layer for Windows, macOS, and Linux. This Skill package intentionally does not provide Windows PowerShell, Windows CMD, or POSIX shell wrapper entrypoints for Runtime/Agent execution; avoiding OS-specific wrappers keeps the Skill contract identical on every platform and ensures all authentication, REST routing, payload parsing, and error handling stay in `manage-config.mjs`.

# Authentication and payloads

Normal self-management usage should authenticate through a UTF-8 login properties file supplied by the user. **Current-workspace convention:** the user has already supplied `<workspace-root>/properties.txt` (currently `G:\project\agent\properties.txt`), which contains `username` and `password`; reuse that known path for this workspace instead of repeatedly asking the user for it. Pass only its path with `--login-file <path>` and never print, copy, commit, or otherwise disclose the file's contents. `SPREAD_AI_RUNTIME_LOGIN_FILE` or an explicit `--login-file` overrides this convention. When the Skill moves to another workspace, retain the relative `<workspace-root>/properties.txt` convention when that file exists; otherwise ask the user for a properties-file path. The file must contain `username=...` and `password=...`; it may also contain `runtime_url=http://127.0.0.1:<port>`, `device_id=spread-ai-config-management`, and `client_kind=builtin-management-skill`. Do not ask for or pass username/password as command-line arguments.

The Node client calls `POST /api/auth/login`, uses the returned token only in memory as `Authorization: Bearer <token>` for the requested configuration REST call, and then makes a best-effort `POST /api/auth/logout` to revoke the script-owned session. It never prints properties content, passwords, or bearer tokens; never uses query-string tokens; and never reads PostgreSQL, SQLite, YAML, browser storage, or local Runtime configuration files to discover auth secrets.

Advanced diagnostic token sources remain available: explicit `--token-file`, `--token-stdin`, and `--auth-file` take precedence over `--login-file`; environment token/token-file/auth-file variables are fallback paths after login-file. If no auth source is provided, the script returns structured JSON with `code: "auth_required"`. Login failure and rate limiting also return structured JSON errors without credential values.

For writes, place UTF-8 JSON or supported YAML payloads in `--file <path>` or pass `--file -` to read the payload from stdin. JSON is the baseline cross-platform input format. The Node core owns the optional YAML parsing path; wrappers must not parse or transform complex payloads. Use `list` for summaries and `get --id <id>` for detail. Use `tool list` to discover public local Runtime tool IDs and `tool get --id <tool_name>` to read a tool's detail/schema before placing the name in an Agent definition `tool_ids` array. For child-resource operations such as provider model bindings, execution-policy candidates, Agent MCP/Skill relations, or prompt content, pass the parent `--id` and a slash-separated `--sub-path` that maps to the REST child path, then use `create`, `update`, `patch`, `delete`, or `reorder` as appropriate.
