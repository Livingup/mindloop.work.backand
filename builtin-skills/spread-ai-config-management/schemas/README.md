# Schemas

This directory is reserved for JSON Schema files describing REST payloads accepted by the local Spread AI Runtime configuration endpoints.

The cross-platform management script accepts UTF-8 JSON payloads as the baseline format and also supports a simple YAML payload path in the Node core. Both formats are converted to the JSON request body sent to Runtime REST endpoints. Runtime REST adapters remain the source of truth for validation and business rules.

The script is intentionally a thin REST client. It does not read or write PostgreSQL, YAML, SQLite, browser localStorage, or local Runtime configuration storage directly.

Authentication is supplied by the caller through a UTF-8 login properties file for normal built-in usage. The file contains `username=...` and `password=...`, with optional `runtime_url=...`, `device_id=...`, and `client_kind=...`. The script posts those credentials to `/api/auth/login`, uses the returned token only in memory as `Authorization: Bearer <token>` for the requested configuration REST operation, and performs best-effort `/api/auth/logout` afterwards. Explicit `--token-file`, `--token-stdin`, `--auth-file`, and token environment variables are diagnostic/development fallback paths. Passwords and tokens must not be placed in query strings or command-line arguments and must not be printed.
