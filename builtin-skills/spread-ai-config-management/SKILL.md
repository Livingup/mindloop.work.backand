---
name: spread-ai-config-management
description: Manage Spread AI configuration through the local Runtime REST API. Use for Agent definitions, MCP service configuration, LLM model/provider/execution policy configuration, prompt snippets, and read-only Skill metadata. Do not use for runtime task lifecycle, tool execution, remote MCP consumption, logs, terminal, file resources, Scene, communication, or context-compression operations.
compatibility: Windows PowerShell 5.1+; requires the Spread AI Runtime backend to be running and reachable on localhost.
metadata:
  display-name: Spread AI Configuration Management
  version: "1.1.0"
  managed-by: spread-ai-runtime
  entrypoint.windows-powershell: scripts/manage-config.ps1
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

# Script

Windows release includes:

```powershell
scripts/manage-config.ps1 <resource> <action> [options]
```

Authentication secrets must be supplied through `--token-file`, `--token-stdin`, `--auth-file`, or environment variables. Do not place passwords, API keys, or bearer tokens directly in command-line arguments.

For writes, place JSON or YAML payloads in `--file <path>` or pass `--file -` to read from stdin. The script converts YAML payloads to JSON before calling the REST API. Use `list` for summaries and `get --id <id>` for detail. For child-resource operations such as provider model bindings, execution-policy candidates, Agent MCP/Skill relations, or prompt content, pass the parent `--id` and a slash-separated `--sub-path` that maps to the REST child path, then use `create`, `update`, `patch`, `delete`, or `reorder` as appropriate.
