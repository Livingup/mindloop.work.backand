# -*- coding: utf-8 -*-
"""Strictly redacted diagnostics for the louyi-codex integration."""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from typing import Any

logger = logging.getLogger("agent_runtime.codex_sdk")

_UPSTREAM_REQUEST_ID_HEADERS = {
    "x-request-id",
    "request-id",
    "openai-request-id",
    "cf-ray",
}
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:/#…\-\[\]]+$")
_SAFE_PROXY_HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:%\-\[\]]+$")
_SAFE_DIAGNOSTIC_KEYS = frozenset({
    "stage",
    "error_type",
    "code",
    "message",
    "http_status",
    "retryable",
    "retry_at",
    "partial_output",
    "request_id",
    "attempt_id",
    "account_id",
    "upstream_request_id",
    "transport_subtype",
    "details",
    "proxy",
})
_PROXY_ERROR_CODES = frozenset({"proxy_error"})
_CONNECT_ERROR_CODES = frozenset({"connect_error"})
_TIMEOUT_ERROR_CODES = frozenset({"request_timeout", "stream_idle_timeout"})
_HTTP_ERROR_CODES = frozenset({"upstream_http_error"})
_HTTPX_PROXY_CAUSE_TYPES = frozenset({"ProxyError"})
_HTTPX_CONNECT_CAUSE_TYPES = frozenset({"ConnectError", "NetworkError"})
_HTTPX_TIMEOUT_CAUSE_TYPES = frozenset({
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
})
_HTTPX_HTTP_CAUSE_TYPES = frozenset({"HTTPStatusError"})
_NON_JSON_CAUSE_TYPES = frozenset({"ValueError", "JSONDecodeError"})


def short_identifier(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) <= 4:
        return f"#{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
    if len(text) <= 8:
        return f"{text[:2]}…{text[-2:]}"
    if len(text) <= 12:
        return f"{text[:3]}…{text[-3:]}"
    return f"{text[:6]}…{text[-4:]}"


def stable_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def proxy_diagnostic(value: Any) -> dict[str, Any]:
    url = getattr(value, "url", value)
    if not isinstance(url, str) or not url.strip():
        return {"proxy_configured": False}
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return {
            "proxy_configured": True,
            "proxy_scheme": None,
            "proxy_host": None,
            "proxy_port": None,
        }
    return {
        "proxy_configured": True,
        "proxy_scheme": parsed.scheme.lower() or None,
        "proxy_host": parsed.hostname,
        "proxy_port": port,
    }


def sdk_error_projection(
    error: Any,
    *,
    stage: str,
    proxy: Any = None,
    request_id: str | None = None,
    attempt_id: str | None = None,
    account_id: str | None = None,
    upstream_request_id: str | None = None,
    partial_output: bool | None = None,
) -> dict[str, Any]:
    """Project one SDK/Runtime failure into a bounded allowlisted diagnostic DTO.

    The projection never copies SDK messages, causes, URLs, headers, bodies, prompts,
    tools, SSE payloads, tokens, OAuth state/code, or arbitrary details.
    """
    existing = _existing_diagnostic(error)
    source = getattr(error, "sdk_error", None) or error
    error_type = _safe_identifier(source.__class__.__name__, default="CodexSDKError")
    default_code = error_type if error_type in {"ValueError", "TypeError"} else "codex_sdk_error"
    code = _safe_identifier(getattr(source, "code", None), default=default_code)
    http_status = _safe_http_status(getattr(source, "http_status", None))
    if http_status is None:
        http_status = _safe_http_status(existing.get("http_status"))
    transport_subtype = _transport_subtype(source, code=code, http_status=http_status)
    if transport_subtype is None:
        transport_subtype = _safe_transport_subtype(existing.get("transport_subtype"))
    safe_stage = _safe_identifier(existing.get("stage"), default=None) or _safe_identifier(stage, default="codex")
    projected_details = _project_details(getattr(source, "details", None))
    if projected_details is None:
        projected_details = _project_details(existing.get("details"), already_projected=True)
    retry_at = _safe_datetime(getattr(source, "retry_at", None)) or _safe_datetime(existing.get("retry_at"))
    projected_partial = (
        bool(partial_output)
        if partial_output is not None
        else bool(
            getattr(error, "partial_output", False)
            or getattr(source, "partial_output", False)
            or existing.get("partial_output", False)
        )
    )
    result: dict[str, Any] = {
        "stage": safe_stage,
        "error_type": error_type,
        "code": code,
        "message": safe_sdk_error_message(code, transport_subtype=transport_subtype),
        "http_status": http_status,
        "retryable": bool(getattr(source, "retryable", existing.get("retryable", False))),
        "retry_at": retry_at,
        "partial_output": projected_partial,
        "request_id": _project_identifier(request_id, getattr(source, "request_id", None), existing.get("request_id")),
        "attempt_id": _project_identifier(attempt_id, getattr(source, "attempt_id", None), existing.get("attempt_id")),
        "account_id": _project_identifier(account_id, getattr(source, "account_id", None), existing.get("account_id")),
        "upstream_request_id": _project_identifier(upstream_request_id, getattr(source, "upstream_request_id", None), existing.get("upstream_request_id")),
        "transport_subtype": transport_subtype,
        "details": projected_details,
    }
    projected_proxy = _project_proxy(proxy) if proxy is not None else _project_proxy(existing.get("proxy"))
    if projected_proxy is not None:
        result["proxy"] = projected_proxy
    return result


def sdk_error_diagnostic(error: Any, *, stage: str = "codex") -> dict[str, Any]:
    """Compatibility wrapper; new call sites should pass an explicit stage."""
    return sdk_error_projection(error, stage=stage)


def safe_sdk_error_message(code: str, *, transport_subtype: str | None = None) -> str:
    if code == "transport_error":
        return {
            "proxy": "Codex proxy request failed.",
            "connect": "Codex network connection failed.",
            "timeout": "Codex request timed out.",
            "http": "Codex upstream HTTP request failed.",
            "non_json": "Codex upstream returned a non-JSON response.",
            "unknown": "Codex transport failed.",
        }.get(transport_subtype or "unknown", "Codex transport failed.")
    messages = {
        "account_not_found": "Codex account was not found.",
        "account_conflict": "Codex account state changed; refresh and retry.",
        "account_disabled": "Codex account is disabled.",
        "no_available_account": "No Codex account is currently available.",
        "auth_attempt_not_found": "Codex authorization attempt was not found.",
        "auth_attempt_expired": "Codex authorization attempt expired.",
        "auth_attempt_cancelled": "Codex authorization attempt was cancelled.",
        "auth_attempt_already_completed": "Codex authorization attempt already completed.",
        "auth_attempt_conflict": "Codex authorization attempt state changed.",
        "oauth_state_mismatch": "Codex OAuth callback validation failed.",
        "oauth_exchange": "Codex OAuth exchange failed.",
        "proxy_error": "Codex proxy request failed.",
        "connect_error": "Codex network connection failed.",
        "request_timeout": "Codex request timed out.",
        "stream_idle_timeout": "Codex response stream timed out.",
        "upstream_http_error": "Codex upstream HTTP request failed.",
        "operation_not_found": "Codex account operation was not found.",
        "operation_wait_timeout": "Waiting for the Codex account operation timed out.",
        "request_cancelled": "Codex request was cancelled.",
        "validation_error": "Codex request parameters are invalid.",
        "configuration_error": "louyi-codex SDK configuration is invalid.",
        "reserved_response_field": "Codex request contains an SDK-owned or reserved field.",
        "unsupported_response_parameter": "Codex request contains an unsupported public Responses parameter.",
        "client_closed": "louyi-codex SDK is not ready.",
        "partial_stream_error": "Codex response stream failed after output began.",
        "protocol_error": "Codex response protocol failed.",
        "sse_protocol_error": "Codex response stream protocol failed.",
        "storage_error": "louyi-codex account storage failed.",
        "ValueError": "Codex request parameters are invalid.",
        "TypeError": "Codex request parameter types are invalid.",
    }
    return messages.get(code, f"louyi-codex operation failed ({code}).")


def _existing_diagnostic(error: Any) -> dict[str, Any]:
    value = getattr(error, "diagnostic", None)
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if str(key) in _SAFE_DIAGNOSTIC_KEYS}


def _transport_subtype(error: Any, *, code: str, http_status: int | None) -> str | None:
    error_type = error.__class__.__name__
    if code in _PROXY_ERROR_CODES or error_type == "ProxyError":
        return "proxy"
    if code in _CONNECT_ERROR_CODES or error_type == "ConnectError":
        return "connect"
    if code in _TIMEOUT_ERROR_CODES or error_type in {"RequestTimeoutError", "StreamIdleTimeoutError"}:
        return "timeout"
    if code in _HTTP_ERROR_CODES or error_type == "UpstreamHTTPError" or http_status is not None:
        return "http"
    if code != "transport_error" and error_type != "TransportError":
        return None
    cause = getattr(error, "__cause__", None)
    if cause is None:
        return "unknown"
    cause_type = cause.__class__.__name__
    cause_module = cause.__class__.__module__
    is_httpx_cause = cause_module == "httpx" or cause_module.startswith("httpx.")
    if is_httpx_cause and cause_type in _HTTPX_PROXY_CAUSE_TYPES:
        return "proxy"
    if is_httpx_cause and cause_type in _HTTPX_TIMEOUT_CAUSE_TYPES:
        return "timeout"
    if is_httpx_cause and cause_type in _HTTPX_CONNECT_CAUSE_TYPES:
        return "connect"
    if is_httpx_cause and cause_type in _HTTPX_HTTP_CAUSE_TYPES:
        return "http"
    if (cause_module == "builtins" or cause_module == "json.decoder") and cause_type in _NON_JSON_CAUSE_TYPES:
        return "non_json"
    return "unknown"


def _project_details(
    value: Any,
    *,
    already_projected: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("field", "validator", "parameter", "reason_code"):
        projected = _safe_identifier(value.get(key), default=None)
        if projected is not None:
            result[key] = projected
    path = value.get("path")
    if isinstance(path, (list, tuple)):
        projected_path: list[str | int] = []
        for item in path[:16]:
            if isinstance(item, bool):
                continue
            if isinstance(item, int) and 0 <= item <= 1_000_000:
                projected_path.append(item)
            else:
                projected = _safe_identifier(item, default=None, max_length=80)
                if projected is not None:
                    projected_path.append(projected)
        if projected_path:
            result["path"] = projected_path
    if "validator_value" in value:
        shape = (
            _project_shape_summary(value.get("validator_value"))
            if already_projected
            else _shape_summary(value.get("validator_value"))
        )
        if shape is not None:
            result["validator_value"] = shape
    for key in ("total", "enabled", "disabled", "available", "rate_limited", "exhausted", "error", "unknown"):
        item = value.get(key)
        if (
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 1_000_000_000
        ):
            result[key] = item
    earliest_retry_at = _safe_datetime(value.get("earliest_retry_at"))
    if earliest_retry_at is not None:
        result["earliest_retry_at"] = earliest_retry_at
    return result or None


def _shape_summary(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": _safe_identifier(value.__class__.__name__, default="unknown", max_length=80)
    }
    if isinstance(value, (str, bytes, bytearray, list, tuple, set, frozenset, Mapping)):
        length = len(value)
        if length <= 1_000_000_000:
            result["length"] = length
    return result


def _project_shape_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    value_type = _safe_identifier(value.get("type"), default=None, max_length=80)
    if value_type is None:
        return None
    result: dict[str, Any] = {"type": value_type}
    length = value.get("length")
    if (
        isinstance(length, int)
        and not isinstance(length, bool)
        and 0 <= length <= 1_000_000_000
    ):
        result["length"] = length
    return result


def _project_proxy(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping) and "configured" in value:
        configured = bool(value.get("configured"))
        if not configured:
            return {"configured": False, "scheme": None, "host": None, "port": None}
        return {
            "configured": True,
            "scheme": _safe_identifier(value.get("scheme"), default=None, max_length=32),
            "host": _safe_proxy_host(value.get("host")),
            "port": _safe_port(value.get("port")),
        }
    safe = proxy_diagnostic(value)
    return {
        "configured": bool(safe.get("proxy_configured")),
        "scheme": _safe_identifier(safe.get("proxy_scheme"), default=None, max_length=32),
        "host": _safe_proxy_host(safe.get("proxy_host")),
        "port": _safe_port(safe.get("proxy_port")),
    }


def _safe_identifier(value: Any, *, default: str | None, max_length: int = 128) -> str | None:
    if value is None:
        return default
    text = str(getattr(value, "value", value)).strip()
    if not text or len(text) > max_length or _SAFE_IDENTIFIER_PATTERN.fullmatch(text) is None:
        return default
    return text


def _safe_proxy_host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > 255
        or _SAFE_PROXY_HOST_PATTERN.fullmatch(text) is None
    ):
        return None
    return text


def _safe_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _safe_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _safe_transport_subtype(value: Any) -> str | None:
    return str(value) if value in {"proxy", "connect", "timeout", "http", "non_json", "unknown"} else None


def _safe_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return text


def _first_identifier(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _project_identifier(explicit: Any, source: Any, existing: Any) -> str | None:
    raw = _first_identifier(explicit, source)
    if raw is not None:
        return short_identifier(raw)
    return _safe_identifier(existing, default=None)


class CodexSdkRawEventSink:
    """Logs request lifecycle metadata without raw headers, bodies, prompts, or SSE payloads."""

    def emit(self, event: Any) -> None:
        try:
            payload = getattr(event, "payload", None)
            kind = str(getattr(getattr(event, "kind", None), "value", getattr(event, "kind", "unknown")))
            fields: dict[str, Any] = {
                "kind": kind,
                "scope": str(getattr(getattr(event, "scope", None), "value", getattr(event, "scope", "unknown"))),
                "request_id": short_identifier(getattr(event, "request_id", None)),
                "attempt_id": short_identifier(getattr(event, "attempt_id", None)),
                "account_id": short_identifier(getattr(event, "account_id", None)),
                "sequence": getattr(event, "sequence", None),
            }
            if kind == "client_request":
                parameters = getattr(payload, "parameters", {}) or {}
                fields.update({
                    "model": parameters.get("model") if isinstance(parameters, dict) else None,
                    "standard_fields": sorted(parameters.keys()) if isinstance(parameters, dict) else [],
                    "input_items": _item_count(parameters.get("input")) if isinstance(parameters, dict) else None,
                    "tool_count": _item_count(parameters.get("tools")) if isinstance(parameters, dict) else None,
                })
            elif kind == "request_prepared":
                fields.update({
                    "method": getattr(payload, "method", None),
                    "header_count": _item_count(getattr(payload, "headers", ())),
                })
            elif kind == "request_body":
                fields["body_bytes"] = len(getattr(payload, "body", b"") or b"")
            elif kind == "response_headers":
                headers = getattr(payload, "headers", ())
                fields.update({
                    "status_code": getattr(payload, "status_code", None),
                    "header_count": _item_count(headers),
                    "upstream_request_id": _request_id_header(headers),
                })
            elif kind == "response_body_chunk":
                fields["chunk_bytes"] = len(getattr(payload, "chunk", b"") or b"")
            elif kind == "sse_frame":
                fields.update({
                    "frame_bytes": len(getattr(payload, "frame", b"") or b""),
                    "data_bytes": len(getattr(payload, "data", b"") or b""),
                })
            elif kind == "parsed_event":
                parsed_event = getattr(payload, "event", None)
                fields["event_type"] = getattr(parsed_event, "type", None)
            elif kind == "terminal":
                terminal_error = getattr(payload, "error", None)
                fields.update({
                    "status": getattr(payload, "status", None),
                    "error": (
                        sdk_error_projection(
                            terminal_error,
                            stage="responses_terminal",
                            request_id=getattr(event, "request_id", None),
                            attempt_id=getattr(event, "attempt_id", None),
                            account_id=getattr(event, "account_id", None),
                        )
                        if terminal_error is not None
                        else None
                    ),
                })
            logger.info("codex_sdk.raw_event %s", fields)
        except Exception as exc:
            logger.error(
                "codex_sdk.raw_event_logging_failed error_type=%s",
                exc.__class__.__name__,
            )


def _request_id_header(headers: Any) -> str | None:
    for item in headers or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        name, value = str(item[0]).lower(), str(item[1])
        if name in _UPSTREAM_REQUEST_ID_HEADERS:
            return short_identifier(value)
    return None


def _item_count(value: Any) -> int | None:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return 1
