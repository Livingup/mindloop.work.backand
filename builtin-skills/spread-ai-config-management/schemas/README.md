# Schemas

This directory is reserved for JSON Schema files describing REST payloads accepted by the local Spread AI Runtime configuration endpoints.

The cross-platform management script accepts UTF-8 JSON payloads as the baseline format and also supports a simple YAML payload path in the Node core. Both formats are converted to the JSON request body sent to Runtime REST endpoints. Runtime REST adapters remain the source of truth for validation and business rules.

The script is intentionally a thin REST client. It does not read or write PostgreSQL, YAML, SQLite, or local Runtime configuration storage directly.
