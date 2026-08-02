# -*- coding: utf-8 -*-
"""Strictly redacted diagnostics for the louyi-codex integration."""
from __future__ import annotations

import hashlib
import logging
import urllib.parse
from typing import Any

logger = logging.getLogger("agent_runtime.codex_sdk")

_UPSTREAM_REQUEST_ID_HEADERS = {
    "x-request-id",
    "request-id",
    "openai-request-id",
    "cf-ray",
}


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


def sdk_error_diagnostic(error: BaseException | None) -> dict[str, Any]:
    if error is None:
        return {}
    return {
        "error_type": error.__class__.__name__,
        "error_code": getattr(error, "code", None),
        "http_status": getattr(error, "http_status", None),
        "request_id": short_identifier(getattr(error, "request_id", None)),
        "attempt_id": short_identifier(getattr(error, "attempt_id", None)),
        "account_id": short_identifier(getattr(error, "account_id", None)),
        "upstream_request_id": short_identifier(getattr(error, "upstream_request_id", None)),
        "retryable": bool(getattr(error, "retryable", False)),
        "partial_output": bool(getattr(error, "partial_output", False)),
    }


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
                    "url": _safe_url(getattr(payload, "url", None)),
                    "header_names": _header_names(getattr(payload, "headers", ())),
                })
            elif kind == "request_body":
                fields["body_bytes"] = len(getattr(payload, "body", b"") or b"")
            elif kind == "response_headers":
                headers = getattr(payload, "headers", ())
                fields.update({
                    "status_code": getattr(payload, "status_code", None),
                    "header_names": _header_names(headers),
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
                    "error_code": getattr(terminal_error, "code", None),
                    "http_status": getattr(terminal_error, "http_status", None),
                    "retryable": getattr(terminal_error, "retryable", None),
                })
            logger.info("codex_sdk.raw_event %s", fields)
        except Exception as exc:
            logger.error(
                "codex_sdk.raw_event_logging_failed error_type=%s",
                exc.__class__.__name__,
            )


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _header_names(headers: Any) -> list[str]:
    result: list[str] = []
    for item in headers or ():
        if isinstance(item, (tuple, list)) and item:
            result.append(str(item[0]).lower())
    return sorted(set(result))


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
