# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import logging
import re
import urllib.parse
from dataclasses import replace
from typing import Any

from agent_runtime.llm_http_trace.config import LlmHttpTraceConfig
from agent_runtime.llm_http_trace.models import (
    LLM_HTTP_TRACE_STATUS_COMPLETED,
    LLM_HTTP_TRACE_STATUS_FAILED,
    LLM_HTTP_TRACE_STATUS_STARTED,
    LlmHttpTraceContext,
    LlmHttpTraceLifecycleEvent,
    LlmHttpTraceRecord,
    LlmHttpTraceUpstreamAttempt,
    build_started_record,
    utc_now,
)
from agent_runtime.llm_http_trace.repository import LlmHttpTraceRepository

logger = logging.getLogger(__name__)

_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-goog-api-key",
    "api-key",
    "anthropic-api-key",
    "chatgpt-account-id",
    "cookie",
    "set-cookie",
    "session-id",
    "x-session-id",
    "x-access-token",
    "x-auth-token",
    "oauth-token",
    "refresh-token",
}
_SENSITIVE_QUERY_KEYS = {"key", "api_key", "apikey", "access_token", "token"}
_STRICT_SENSITIVE_QUERY_KEYS = _SENSITIVE_QUERY_KEYS | {
    "authorization",
    "code",
    "credential",
    "credentials",
    "id_token",
    "oauth_code",
    "oauth_state",
    "password",
    "proxy_password",
    "proxy_username",
    "refresh_token",
    "secret",
    "state",
    "username",
}
_UPSTREAM_LIFECYCLE_KINDS = frozenset({
    "request_prepared",
    "request_body",
    "response_headers",
    "response_body_chunk",
    "sse_frame",
    "terminal",
})
_UPSTREAM_TERMINAL_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled"})
_SAFE_TRACE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/#\-]+$")
_JWT_LIKE_VALUE = re.compile(r"^[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_SECRET_PREFIX_VALUE = re.compile(r"^(?:sk|pk|sess|oauth|token)[-_][A-Za-z0-9._-]{12,}$", re.IGNORECASE)
_INLINE_SECRET_VALUE = re.compile(
    r"(?:authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|oauth[-_ ]?(?:state|code|token)|proxy[-_ ]?(?:username|password)|session[-_ ]?id|account[-_ ]?id|password|credential)\s*[:=]",
    re.IGNORECASE,
)
_SAFE_PROTOCOL_VALUE = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")
_SAFE_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SSE_BOUNDARY = re.compile(br"\r\n\r\n|\n\n|\r\r")
_REDACTED = "[REDACTED]"
_REDACTED_RAW_BODY = "[REDACTED RAW BODY]"
_MAX_JSON_REDACTION_DEPTH = 48
_MAX_SAFE_SCALAR_LENGTH = 4096
_MAX_PROJECTED_BODY_TEXT_BYTES = 2 * 1024 * 1024
_SENSITIVE_COMPACT_KEYS = frozenset({
    "access_token",
    "account_id",
    "api_key",
    "authorization_code",
    "chatgpt_account_id",
    "client_secret",
    "code_challenge",
    "code_verifier",
    "id_token",
    "oauth_code",
    "oauth_state",
    "oauth_token",
    "prompt_cache_key",
    "proxy_authorization",
    "proxy_password",
    "proxy_username",
    "refresh_token",
    "session_id",
    "set_cookie",
    "x_api_key",
    "x_session_id",
})
_SENSITIVE_COMPACT_KEY_FORMS = frozenset(
    item.replace("_", "") for item in _SENSITIVE_COMPACT_KEYS
)
_SENSITIVE_BODY_KEYS = frozenset({
    "account_id",
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "authorization_code",
    "chatgpt_account_id",
    "client_secret",
    "code",
    "code_challenge",
    "code_verifier",
    "cookie",
    "credential",
    "credentials",
    "id_token",
    "oauth_code",
    "oauth_state",
    "oauth_token",
    "password",
    "passwd",
    "prompt_cache_key",
    "proxy_authorization",
    "proxy_password",
    "proxy_username",
    "refresh_token",
    "secret",
    "session_id",
    "set_cookie",
    "state",
    "token",
    "user",
    "username",
    "x_api_key",
    "x_session_id",
})
_PRIVATE_BODY_CONTEXT_KEYS = frozenset({
    "annotations",
    "arguments",
    "audio",
    "content",
    "data",
    "delta",
    "description",
    "detail",
    "details",
    "encrypted_content",
    "error",
    "function",
    "function_call",
    "image_url",
    "input",
    "input_text",
    "instructions",
    "item",
    "items",
    "logprobs",
    "message",
    "messages",
    "metadata",
    "obfuscation",
    "output",
    "output_text",
    "parsed",
    "part",
    "payload",
    "prompt",
    "reasoning",
    "refusal",
    "result",
    "summary",
    "text",
    "transcript",
    "tool_call",
    "tool_calls",
    "tool_choice",
    "tools",
})
_PRIVATE_BODY_CONTEXT_KEY_FORMS = frozenset(
    item.replace("_", "") for item in _PRIVATE_BODY_CONTEXT_KEYS
)
_SAFE_PRIVATE_CONTEXT_KEYS = frozenset({
    "content_index",
    "event",
    "finish_reason",
    "format",
    "kind",
    "mode",
    "object",
    "output_index",
    "role",
    "sequence_number",
    "status",
    "type",
})


class LlmHttpTraceService:
    def __init__(self, repository: LlmHttpTraceRepository, config: LlmHttpTraceConfig) -> None:
        self._repository = repository
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def get(self, llm_request_id: str) -> LlmHttpTraceRecord | None:
        return self._repository.get(llm_request_id)

    def record_upstream_attempts(
        self,
        *,
        context: LlmHttpTraceContext,
        attempts: tuple[LlmHttpTraceUpstreamAttempt, ...],
    ) -> bool:
        """Persist complete SDK callback snapshots under one logical request ID."""
        if not self.enabled or not attempts:
            return False
        try:
            callback_attempts = tuple(attempts)
            active = callback_attempts[-1]
            started = build_started_record(
                context=context,
                method=active.method,
                url=active.url,
                request_headers=active.request_headers,
                request_body_text=active.request_body_text,
                request_body_bytes=active.request_body_bytes,
                request_body_truncated=active.request_body_truncated,
                raw_request_text=active.raw_request_text or "",
                is_stream=active.is_stream,
            )
            record = replace(
                started,
                status=active.status,
                status_code=active.status_code,
                response_headers=active.response_headers,
                response_body_text=active.response_body_text,
                response_body_bytes=active.response_body_bytes,
                response_body_truncated=active.response_body_truncated,
                response_content_type=active.response_content_type,
                sse_event_count=active.sse_event_count,
                error_type=active.error_type,
                error_message=active.error_message,
                started_at=active.started_at,
                completed_at=active.completed_at,
                duration_ms=active.duration_ms,
                raw_request_text=active.raw_request_text,
                raw_response_text=active.raw_response_text,
                request_body_redacted=active.request_body_redacted,
                response_body_redacted=active.response_body_redacted,
                upstream_attempts=callback_attempts,
            )
            if record.status == LLM_HTTP_TRACE_STATUS_COMPLETED:
                self._repository.save_completed(record)
            elif record.status == LLM_HTTP_TRACE_STATUS_FAILED:
                self._repository.save_failed(record)
            else:
                self._repository.save_started(record)
            return True
        except Exception:
            logger.exception(
                "保存 SDK 上游 HTTP attempt 日志失败: llm_request_id=%s",
                context.llm_request_id,
            )
            return False

    def record_started(
        self,
        *,
        context: LlmHttpTraceContext,
        method: str,
        url: str,
        request_headers: dict[str, str],
        request_body_bytes: bytes,
        is_stream: bool,
    ) -> LlmHttpTraceRecord | None:
        if not self.enabled:
            return None
        try:
            sanitized_url = sanitize_url(url)
            sanitized_headers = sanitize_headers(request_headers)
            request_body_text = decode_body(request_body_bytes)
            request_body_truncated = False
            raw_request_text = build_raw_request_text(
                method=method,
                url=sanitized_url,
                headers=sanitized_headers,
                body_text=request_body_text,
                body_truncated=request_body_truncated,
            )
            record = build_started_record(
                context=context,
                method=method,
                url=sanitized_url,
                request_headers=sanitized_headers,
                request_body_text=request_body_text,
                request_body_bytes=len(request_body_bytes),
                request_body_truncated=request_body_truncated,
                raw_request_text=raw_request_text,
                is_stream=is_stream,
            )
            self._repository.save_started(record)
            return record
        except Exception:
            logger.exception("保存 LLM HTTP 请求开始日志失败: llm_request_id=%s", context.llm_request_id)
            return None

    def record_completed(
        self,
        *,
        started_record: LlmHttpTraceRecord | None,
        context: LlmHttpTraceContext,
        status_code: int,
        response_headers: dict[str, str],
        response_body_bytes: bytes,
        response_content_type: str | None,
        duration_ms: int,
        sse_event_count: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            base = started_record or self._repository.get(context.llm_request_id)
            if base is None:
                return
            sanitized_headers = sanitize_headers(response_headers)
            response_body_text = decode_body(response_body_bytes)
            response_body_truncated = False
            raw_response_text = build_raw_response_text(
                status_code=status_code,
                headers=sanitized_headers,
                body_text=response_body_text,
                body_truncated=response_body_truncated,
            )
            completed = replace(
                base,
                status=LLM_HTTP_TRACE_STATUS_COMPLETED,
                status_code=status_code,
                response_headers=sanitized_headers,
                response_body_text=response_body_text,
                response_body_bytes=len(response_body_bytes),
                response_body_truncated=response_body_truncated,
                response_content_type=response_content_type,
                sse_event_count=sse_event_count,
                completed_at=utc_now(),
                duration_ms=duration_ms,
                raw_response_text=raw_response_text,
            )
            self._repository.save_completed(completed)
        except Exception:
            logger.exception("保存 LLM HTTP 请求完成日志失败: llm_request_id=%s", context.llm_request_id)

    def record_failed(
        self,
        *,
        started_record: LlmHttpTraceRecord | None,
        context: LlmHttpTraceContext,
        error_type: str,
        error_message: str,
        duration_ms: int,
        status_code: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body_bytes: bytes | None = None,
        response_content_type: str | None = None,
        sse_event_count: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            base = started_record or self._repository.get(context.llm_request_id)
            if base is None:
                return
            sanitized_headers = sanitize_headers(response_headers or {})
            body = response_body_bytes or b""
            response_body_text = decode_body(body)
            response_body_truncated = False
            raw_response_text = None
            if status_code is not None or sanitized_headers or response_body_text:
                raw_response_text = build_raw_response_text(
                    status_code=status_code,
                    headers=sanitized_headers,
                    body_text=response_body_text,
                    body_truncated=response_body_truncated,
                )
            failed = replace(
                base,
                status=LLM_HTTP_TRACE_STATUS_FAILED,
                status_code=status_code,
                response_headers=sanitized_headers,
                response_body_text=response_body_text if body else None,
                response_body_bytes=len(body),
                response_body_truncated=response_body_truncated,
                response_content_type=response_content_type,
                sse_event_count=sse_event_count,
                error_type=error_type,
                error_message=error_message,
                completed_at=utc_now(),
                duration_ms=duration_ms,
                raw_response_text=raw_response_text,
            )
            self._repository.save_failed(failed)
        except Exception:
            logger.exception("保存 LLM HTTP 请求失败日志失败: llm_request_id=%s", context.llm_request_id)


def sanitize_headers(headers: dict[str, str], *, strict: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        text_key = str(key)
        if strict and _SAFE_HEADER_NAME.fullmatch(text_key) is None:
            continue
        if _is_sensitive_header(text_key, strict=strict):
            result[text_key] = "[REDACTED]" if strict else mask_secret(str(value))
        else:
            result[text_key] = _sanitize_header_value(str(value), strict=strict)
    return result


def sanitize_url(url: str, *, strict: bool = False) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return "[REDACTED]" if strict else str(url)
    sanitized_pairs = [
        (key, "[REDACTED]" if _is_sensitive_query_key(key, strict=strict) else value)
        for key, value in query_pairs
    ]
    sanitized_query = urllib.parse.urlencode(sanitized_pairs, doseq=True)
    sanitized_fragment = "[REDACTED]" if strict and parsed.fragment else parsed.fragment
    sanitized_path = _sanitize_url_path(parsed.path, strict=strict)
    return urllib.parse.urlunsplit(
        (parsed.scheme, _sanitize_url_netloc(parsed), sanitized_path, sanitized_query, sanitized_fragment)
    )


def _is_sensitive_header(name: str, *, strict: bool) -> bool:
    normalized = name.strip().lower().replace("_", "-")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return (
        normalized in _SENSITIVE_HEADERS
        or compact in _SENSITIVE_COMPACT_KEY_FORMS
        or "authorization" in normalized
        or "cookie" in normalized
        or normalized.endswith(("-token", "-secret", "-password", "-credential"))
        or (strict and "oauth" in normalized and normalized.endswith(("-state", "-code")))
        or (
            strict
            and (
                normalized in {"openai-organization", "openai-project"}
                or normalized.endswith((
                    "-account-id",
                    "-organization-id",
                    "-project-id",
                    "-user-id",
                ))
            )
        )
    )


def _is_sensitive_query_key(key: str, *, strict: bool) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if (
        normalized in _SENSITIVE_QUERY_KEYS
        or compact in _SENSITIVE_COMPACT_KEY_FORMS
    ):
        return True
    if not strict:
        return False
    return (
        normalized in _STRICT_SENSITIVE_QUERY_KEYS
        or normalized.endswith(("_token", "_secret", "_password", "_credential"))
    )


def _sanitize_url_netloc(parsed: urllib.parse.SplitResult) -> str:
    if parsed.username is None and parsed.password is None:
        return parsed.netloc
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


def _sanitize_url_path(path: str, *, strict: bool) -> str:
    if not strict or not path:
        return path
    segments = path.split("/")
    sanitized: list[str] = []
    for segment in segments:
        decoded = urllib.parse.unquote(segment)
        stripped = decoded.strip()
        lowered = stripped.lower()
        if (
            lowered.startswith(("bearer ", "basic ", "token="))
            or _JWT_LIKE_VALUE.fullmatch(stripped) is not None
            or _SECRET_PREFIX_VALUE.fullmatch(stripped) is not None
            or _INLINE_SECRET_VALUE.search(decoded) is not None
        ):
            sanitized.append(urllib.parse.quote(_REDACTED, safe=""))
        else:
            sanitized.append(segment)
    return "/".join(sanitized)


def _sanitize_header_value(value: str, *, strict: bool) -> str:
    if not strict:
        return value
    text = value.replace("\r", " ").replace("\n", " ")
    stripped = text.strip()
    lowered = stripped.lower()
    if (
        lowered.startswith(("bearer ", "basic ", "token "))
        or _JWT_LIKE_VALUE.fullmatch(stripped) is not None
        or _SECRET_PREFIX_VALUE.fullmatch(stripped) is not None
        or _INLINE_SECRET_VALUE.search(text) is not None
    ):
        return "[REDACTED]"
    if "://" in stripped:
        return sanitize_url(stripped, strict=True)
    return text[:4096]


def mask_secret(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return "[REDACTED]"
    parts = stripped.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in {"bearer", "basic"}:
        return f"{parts[0]} {mask_secret(parts[1])}"
    if len(stripped) <= 8:
        return "[REDACTED]"
    return f"{stripped[:3]}****{stripped[-4:]}"


def decode_body(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def redact_codex_request_body(
    raw_body: bytes,
    *,
    body_present: bool,
    truncated: bool,
) -> str:
    """Return a redacted form of the actual Codex request JSON wire body."""
    if not body_present:
        return ""
    if truncated:
        return _REDACTED_RAW_BODY
    return _redact_json_text(raw_body, invalid_marker=_REDACTED_RAW_BODY, require_object=True)


def redact_codex_response_body(
    raw_body: bytes,
    *,
    sse_frames: tuple[bytes, ...],
    body_present: bool,
    content_type: str | None,
    truncated: bool,
) -> str | None:
    """Return a redacted form of actual Codex response JSON or complete SSE frames."""
    if not body_present:
        return None
    if sse_frames:
        # Frames and response chunks describe the same wire bytes; render frames once.
        return _redact_sse_frames(sse_frames)
    if not raw_body:
        return ""
    if _is_sse_content_type(content_type) or _looks_like_sse(raw_body):
        frames = _split_sse_wire(raw_body, include_tail=not truncated)
        return _redact_sse_frames(frames) if frames else _REDACTED
    if truncated:
        return _REDACTED
    return _redact_json_text(raw_body, invalid_marker=_REDACTED, require_object=False)


def _redact_json_text(
    value: bytes | str,
    *,
    invalid_marker: str,
    require_object: bool,
    private_sse_root: bool = False,
) -> str:
    parsed_ok, parsed = _load_wire_json(value)
    if not parsed_ok or (require_object and not isinstance(parsed, dict)):
        return invalid_marker
    if not isinstance(parsed, (dict, list)):
        return invalid_marker
    redacted = _redact_json_value(
        parsed,
        private_context=private_sse_root and _is_private_sse_payload(parsed),
    )
    try:
        return json.dumps(redacted, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return invalid_marker


def _is_private_sse_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = value.get("type")
    return isinstance(event_type, str) and (
        event_type == "response.metadata" or event_type.startswith("codex.")
    )


def _load_wire_json(value: bytes | str) -> tuple[bool, Any]:
    try:
        text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value
        return True, json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return False, None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _redact_json_value(
    value: Any,
    *,
    private_context: bool = False,
    key: str | None = None,
    depth: int = 0,
) -> Any:
    if depth > _MAX_JSON_REDACTION_DEPTH:
        return _REDACTED
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            item_key = str(raw_key)
            normalized = _normalize_body_key(item_key)
            if _is_sensitive_body_key(normalized):
                result[item_key] = _REDACTED
                continue
            result[item_key] = _redact_json_value(
                item,
                private_context=(
                    private_context
                    or normalized in _PRIVATE_BODY_CONTEXT_KEYS
                    or normalized.replace("_", "") in _PRIVATE_BODY_CONTEXT_KEY_FORMS
                ),
                key=item_key,
                depth=depth + 1,
            )
        return result
    if isinstance(value, list):
        return [
            _redact_json_value(
                item,
                private_context=private_context,
                key=key,
                depth=depth + 1,
            )
            for item in value
        ]
    normalized_key = _normalize_body_key(key or "")
    if (
        normalized_key in _SAFE_PRIVATE_CONTEXT_KEYS
        and not _is_safe_private_context_scalar(normalized_key, value)
    ):
        return _REDACTED
    if private_context and not _is_safe_private_context_scalar(normalized_key, value):
        return _REDACTED
    if isinstance(value, str):
        return _sanitize_body_scalar(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _REDACTED


def _normalize_body_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _is_sensitive_body_key(normalized: str) -> bool:
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_BODY_KEYS
        or compact in _SENSITIVE_COMPACT_KEY_FORMS
        or "password" in compact
        or "credential" in compact
        or compact.endswith("secret")
        or (
            "token" in compact
            and not compact.endswith(("tokens", "tokencount", "tokenscount"))
        )
        or normalized == "id"
        or normalized.endswith((
            "_account_id",
            "_api_key",
            "_credential",
            "_id",
            "_password",
            "_secret",
            "_session_id",
            "_token",
        ))
        or ("oauth" in normalized and normalized.endswith(("code", "state", "token", "secret")))
        or (normalized.startswith("proxy_") and normalized.endswith(("authorization", "password", "username")))
    )


def _is_safe_private_context_scalar(normalized_key: str, value: Any) -> bool:
    if normalized_key not in _SAFE_PRIVATE_CONTEXT_KEYS:
        return False
    if isinstance(value, str):
        return _SAFE_PROTOCOL_VALUE.fullmatch(value.strip()) is not None
    return value is None or isinstance(value, (bool, int, float))


def _sanitize_body_scalar(value: str) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if (
        len(value) > _MAX_SAFE_SCALAR_LENGTH
        or lowered.startswith(("bearer ", "basic ", "token "))
        or _JWT_LIKE_VALUE.fullmatch(stripped) is not None
        or _SECRET_PREFIX_VALUE.fullmatch(stripped) is not None
        or _INLINE_SECRET_VALUE.search(value) is not None
    ):
        return _REDACTED
    if "://" in value:
        return sanitize_url(value, strict=True)
    return value


def _is_sse_content_type(content_type: str | None) -> bool:
    return bool(content_type and "text/event-stream" in content_type.lower())


def _looks_like_sse(value: bytes | str) -> bool:
    prefix = value.lstrip()[:4096]
    if isinstance(prefix, bytes):
        return prefix.startswith((b"data:", b"event:", b":")) or b"\ndata:" in prefix
    return prefix.startswith(("data:", "event:", ":")) or "\ndata:" in prefix


def _split_sse_wire(raw_body: bytes, *, include_tail: bool) -> tuple[bytes, ...]:
    frames: list[bytes] = []
    offset = 0
    for match in _SSE_BOUNDARY.finditer(raw_body):
        frames.append(raw_body[offset:match.end()])
        offset = match.end()
    if include_tail and offset < len(raw_body):
        frames.append(raw_body[offset:])
    return tuple(frame for frame in frames if frame)


def _redact_sse_frames(frames: tuple[bytes, ...]) -> str:
    return "".join(_redact_sse_frame(frame) for frame in frames)


def _redact_sse_frame(frame: bytes) -> str:
    try:
        text = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return f"{_REDACTED}\n\n"
    payload = text.rstrip("\r\n")
    if not payload:
        return "\n\n"
    if payload.strip() in {_REDACTED, _REDACTED_RAW_BODY}:
        return f"{payload.strip()}\n\n"
    rendered: list[str] = []
    data_values: list[str] = []
    data_position: int | None = None
    for line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            continue
        if line.startswith(":"):
            rendered.append(":" if not line[1:].strip() else f": {_REDACTED}")
            continue
        field, separator, raw_value = line.partition(":")
        if not separator:
            raw_value = ""
        value = raw_value[1:] if raw_value.startswith(" ") else raw_value
        safe_field = field if _SAFE_PROTOCOL_VALUE.fullmatch(field) is not None else _REDACTED
        if field == "data":
            if data_position is None:
                data_position = len(rendered)
                rendered.append("")
            data_values.append(value)
        elif field == "event":
            stripped_value = value.strip()
            safe_value = stripped_value if _SAFE_PROTOCOL_VALUE.fullmatch(stripped_value) is not None else _REDACTED
            rendered.append(f"event: {safe_value}" if safe_value else "event:")
        elif field == "retry":
            rendered.append(f"retry: {value}" if value.isdigit() and len(value) <= 16 else f"retry: {_REDACTED}")
        elif field == "id":
            rendered.append("id:" if not value else f"id: {_REDACTED}")
        else:
            rendered.append(f"{safe_field}: {_REDACTED}")
    if data_position is not None:
        data_text = "\n".join(data_values)
        if data_text.strip() == "[DONE]":
            safe_data = "[DONE]"
        else:
            safe_data = _redact_json_text(
                data_text,
                invalid_marker=_REDACTED,
                require_object=False,
                private_sse_root=True,
            )
        rendered[data_position] = f"data: {safe_data}"
    return "\n".join(rendered) + "\n\n"


def build_raw_request_text(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body_text: str,
    body_truncated: bool,
) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    lines = [f"{method.upper()} {path} HTTP/1.1"]
    if parsed.netloc:
        lines.append(f"Host: {parsed.netloc}")
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.append("")
    lines.append(body_text)
    if body_truncated:
        lines.extend(("", "[TRUNCATED]"))
    return "\n".join(lines)


def build_raw_response_text(
    *,
    status_code: int | None,
    headers: dict[str, str],
    body_text: str,
    body_truncated: bool,
) -> str:
    status = status_code if status_code is not None else "unknown"
    lines = [f"HTTP/1.1 {status}"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.append("")
    lines.append(body_text)
    if body_truncated:
        lines.extend(("", "[TRUNCATED]"))
    return "\n".join(lines)


def _build_codex_raw_response_text(
    *,
    status_code: int | None,
    headers: dict[str, str],
    body_text: str,
    body_truncated: bool,
    has_response_evidence: bool,
) -> str | None:
    if not has_response_evidence:
        return None
    if status_code is not None:
        return build_raw_response_text(
            status_code=status_code,
            headers=headers,
            body_text=body_text,
            body_truncated=body_truncated,
        )
    lines = ["[HTTP RESPONSE STATUS NOT RECEIVED]"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.append("")
    lines.append(body_text)
    if body_truncated:
        lines.extend(("", "[TRUNCATED]"))
    return "\n".join(lines)


def _sanitize_codex_request_body_text(
    body_text: str,
    *,
    body_present: bool,
    truncated: bool,
) -> str:
    if not body_present:
        return ""
    if (
        truncated
        or body_text == _REDACTED_RAW_BODY
        or len(body_text.encode("utf-8")) > _MAX_PROJECTED_BODY_TEXT_BYTES
    ):
        return _REDACTED_RAW_BODY
    return _redact_json_text(body_text, invalid_marker=_REDACTED_RAW_BODY, require_object=True)


def _sanitize_codex_response_body_text(
    body_text: str | None,
    *,
    body_present: bool,
    content_type: str | None,
) -> str | None:
    if not body_present:
        return None
    if body_text is None:
        return _REDACTED
    if body_text == "":
        return ""
    if len(body_text.encode("utf-8")) > _MAX_PROJECTED_BODY_TEXT_BYTES:
        return _REDACTED
    if body_text.strip() in {_REDACTED, _REDACTED_RAW_BODY}:
        return _REDACTED
    if _is_sse_content_type(content_type) or _looks_like_sse(body_text):
        frames = _split_sse_wire(body_text.encode("utf-8"), include_tail=True)
        return _redact_sse_frames(frames) if frames else _REDACTED
    return _redact_json_text(body_text, invalid_marker=_REDACTED, require_object=False)


def _sanitize_upstream_attempt(attempt: LlmHttpTraceUpstreamAttempt) -> LlmHttpTraceUpstreamAttempt:
    lifecycle_events = tuple(_sanitize_lifecycle_event(item) for item in attempt.lifecycle_events)
    terminal_event = next((item for item in reversed(lifecycle_events) if item.kind == "terminal"), None)
    terminal_status = terminal_event.status if terminal_event is not None else None
    status = (
        LLM_HTTP_TRACE_STATUS_COMPLETED
        if terminal_status == "completed"
        else LLM_HTTP_TRACE_STATUS_FAILED
        if terminal_status is not None
        else LLM_HTTP_TRACE_STATUS_STARTED
    )
    method = _sanitize_method(attempt.method)
    url = sanitize_url(attempt.url, strict=True)
    request_headers = sanitize_headers(attempt.request_headers, strict=True)
    response_headers = sanitize_headers(attempt.response_headers, strict=True)
    request_body_bytes = _safe_non_negative_int(attempt.request_body_bytes)
    response_body_bytes = _safe_non_negative_int(attempt.response_body_bytes)
    sse_event_count = _safe_non_negative_int(attempt.sse_event_count)
    request_body_present = any(item.kind == "request_body" for item in lifecycle_events)
    response_body_present = any(
        item.kind in {"response_body_chunk", "sse_frame"} for item in lifecycle_events
    )
    request_body_redacted = bool(attempt.request_body_redacted or request_body_present)
    response_body_redacted = bool(attempt.response_body_redacted or response_body_present)
    request_body_truncated = bool(attempt.request_body_truncated)
    response_body_truncated = bool(attempt.response_body_truncated)
    lifecycle_event_count = max(
        len(lifecycle_events),
        _safe_non_negative_int(attempt.lifecycle_event_count),
    )
    lifecycle_truncated = bool(attempt.lifecycle_truncated or lifecycle_event_count > len(lifecycle_events))
    response_content_type = _header_value(response_headers, "content-type")
    request_body_text = _sanitize_codex_request_body_text(
        attempt.request_body_text if isinstance(attempt.request_body_text, str) else "",
        body_present=request_body_present,
        truncated=request_body_truncated,
    )
    response_body_text = _sanitize_codex_response_body_text(
        attempt.response_body_text if isinstance(attempt.response_body_text, str) else None,
        body_present=response_body_present,
        content_type=response_content_type,
    )
    raw_request_text = build_raw_request_text(
        method=method,
        url=url,
        headers=request_headers,
        body_text=request_body_text,
        body_truncated=request_body_truncated,
    )
    status_code = _safe_http_status(attempt.status_code)
    response_lifecycle_present = any(
        item.kind in {"response_headers", "response_body_chunk", "sse_frame"}
        for item in lifecycle_events
    )
    has_response_evidence = bool(
        status_code is not None
        or attempt.response_headers
        or response_headers
        or response_lifecycle_present
        or response_body_bytes > 0
        or (isinstance(attempt.response_body_text, str) and bool(attempt.response_body_text))
        or attempt.response_body_truncated
        or attempt.response_body_redacted
        or sse_event_count > 0
    )
    raw_response_text = _build_codex_raw_response_text(
        status_code=status_code,
        headers=response_headers,
        body_text=response_body_text or "",
        body_truncated=response_body_truncated,
        has_response_evidence=has_response_evidence,
    )
    started_at = attempt.started_at
    completed_at = attempt.completed_at if terminal_status is not None else None
    duration_ms = (
        max(0, int((completed_at - started_at).total_seconds() * 1000))
        if completed_at is not None
        else None
    )
    error_type = None
    error_message = None
    if terminal_status is not None and terminal_status != "completed":
        error_type = (
            _safe_trace_token_value(attempt.error_type)
            or _safe_trace_token_value(terminal_event.error_code if terminal_event is not None else None)
            or "CodexUpstreamAttemptError"
        )
        error_message = f"Codex SDK upstream attempt ended with terminal status '{terminal_status}'."
    return replace(
        attempt,
        sdk_request_id=_safe_trace_token_value(attempt.sdk_request_id) or "[REDACTED]",
        attempt_id=_safe_trace_token_value(attempt.attempt_id) or "[REDACTED]",
        attempt_index=max(1, _safe_non_negative_int(attempt.attempt_index)),
        attempt_kind=attempt.attempt_kind if attempt.attempt_kind in {"initial", "same_account_retry", "failover"} else "unknown",
        account_fingerprint=_safe_trace_token_value(attempt.account_fingerprint),
        status=status,
        method=method,
        url=url,
        request_headers=request_headers,
        request_body_text=request_body_text,
        request_body_bytes=request_body_bytes,
        request_body_truncated=request_body_truncated,
        request_body_redacted=request_body_redacted,
        status_code=status_code,
        response_headers=response_headers,
        response_body_text=response_body_text,
        response_body_bytes=response_body_bytes,
        response_body_truncated=response_body_truncated,
        response_body_redacted=response_body_redacted,
        response_content_type=response_content_type,
        is_stream=True,
        sse_event_count=sse_event_count,
        error_type=error_type,
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        raw_request_text=raw_request_text,
        raw_response_text=raw_response_text,
        lifecycle_events=lifecycle_events,
        lifecycle_event_count=lifecycle_event_count,
        lifecycle_truncated=lifecycle_truncated,
    )


def _sanitize_lifecycle_event(event: LlmHttpTraceLifecycleEvent) -> LlmHttpTraceLifecycleEvent:
    kind = event.kind if event.kind in _UPSTREAM_LIFECYCLE_KINDS else "unknown"
    status = event.status if event.status in _UPSTREAM_TERMINAL_STATUSES else None
    return replace(
        event,
        sequence=max(1, _safe_non_negative_int(event.sequence)),
        kind=kind,
        redacted=bool(event.redacted),
        byte_count=_safe_optional_non_negative_int(event.byte_count),
        data_byte_count=_safe_optional_non_negative_int(event.data_byte_count),
        status=status,
        error_code=_safe_trace_token_value(event.error_code),
    )


def _lifecycle_event_to_dict(event: LlmHttpTraceLifecycleEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "kind": event.kind,
        "timestamp": event.timestamp.isoformat(),
        "redacted": event.redacted,
        "byte_count": event.byte_count,
        "data_byte_count": event.data_byte_count,
        "status": event.status,
        "error_code": event.error_code,
    }


def _upstream_attempt_to_dict(attempt: LlmHttpTraceUpstreamAttempt) -> dict[str, Any]:
    return {
        "sdk_request_id": attempt.sdk_request_id,
        "attempt_id": attempt.attempt_id,
        "attempt_index": attempt.attempt_index,
        "attempt_kind": attempt.attempt_kind,
        "account_fingerprint": attempt.account_fingerprint,
        "status": attempt.status,
        "method": attempt.method,
        "url": attempt.url,
        "request_headers": copy.deepcopy(attempt.request_headers),
        "request_body_text": attempt.request_body_text,
        "request_body_bytes": attempt.request_body_bytes,
        "request_body_truncated": attempt.request_body_truncated,
        "request_body_redacted": attempt.request_body_redacted,
        "status_code": attempt.status_code,
        "response_headers": copy.deepcopy(attempt.response_headers),
        "response_body_text": attempt.response_body_text,
        "response_body_bytes": attempt.response_body_bytes,
        "response_body_truncated": attempt.response_body_truncated,
        "response_body_redacted": attempt.response_body_redacted,
        "response_content_type": attempt.response_content_type,
        "is_stream": attempt.is_stream,
        "sse_event_count": attempt.sse_event_count,
        "error_type": attempt.error_type,
        "error_message": attempt.error_message,
        "started_at": attempt.started_at.isoformat(),
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at is not None else None,
        "duration_ms": attempt.duration_ms,
        "raw_request_text": attempt.raw_request_text,
        "raw_response_text": attempt.raw_response_text,
        "lifecycle_events": [_lifecycle_event_to_dict(item) for item in attempt.lifecycle_events],
        "lifecycle_event_count": attempt.lifecycle_event_count,
        "lifecycle_truncated": attempt.lifecycle_truncated,
        "account_id": attempt.account_id,
        "request_header_pairs": [list(item) for item in attempt.request_header_pairs],
        "request_body_base64": attempt.request_body_base64,
        "response_header_pairs": [list(item) for item in attempt.response_header_pairs],
        "response_body_chunks_base64": list(attempt.response_body_chunks_base64),
        "sse_frames_base64": list(attempt.sse_frames_base64),
        "sse_data_base64": list(attempt.sse_data_base64),
        "sse_frame_bytes": attempt.sse_frame_bytes,
        "sse_data_bytes": attempt.sse_data_bytes,
        "terminal_error": copy.deepcopy(attempt.terminal_error),
        "callback_events": copy.deepcopy(attempt.callback_events),
        "capture_complete": attempt.capture_complete,
        "capture_error": attempt.capture_error,
        "capture_source": attempt.capture_source,
        "socket_level_wire": attempt.socket_level_wire,
    }


def _sanitize_method(method: str) -> str:
    text = str(method).strip().upper()
    return text if text and len(text) <= 32 and text.isalpha() else "[REDACTED]"


def _safe_trace_token_value(value: Any, *, max_length: int = 256) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > max_length or _SAFE_TRACE_TOKEN.fullmatch(text) is None:
        return None
    return text


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _safe_optional_non_negative_int(value: Any) -> int | None:
    return None if value is None else _safe_non_negative_int(value)


def _safe_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _header_value(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def record_to_dict(record: LlmHttpTraceRecord) -> dict[str, Any]:
    return {
        "llm_request_id": record.llm_request_id,
        "llm_call_id": record.llm_call_id,
        "conversation_id": record.conversation_id,
        "task_id": record.task_id,
        "instance_id": record.instance_id,
        "trace_id": record.trace_id,
        "provider_id": record.provider_id,
        "binding_id": record.binding_id,
        "model_id": record.model_id,
        "model_alias": record.model_alias,
        "api_format": record.api_format,
        "candidate_index": record.candidate_index,
        "api_key_index": record.api_key_index,
        "retry_index": record.retry_index,
        "attempt_count": record.attempt_count,
        "status": record.status,
        "method": record.method,
        "url": record.url,
        "request_headers": copy.deepcopy(record.request_headers),
        "request_body_text": record.request_body_text,
        "request_body_bytes": record.request_body_bytes,
        "request_body_truncated": record.request_body_truncated,
        "status_code": record.status_code,
        "response_headers": copy.deepcopy(record.response_headers),
        "response_body_text": record.response_body_text,
        "response_body_bytes": record.response_body_bytes,
        "response_body_truncated": record.response_body_truncated,
        "response_content_type": record.response_content_type,
        "is_stream": record.is_stream,
        "sse_event_count": record.sse_event_count,
        "error_type": record.error_type,
        "error_message": record.error_message,
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at is not None else None,
        "duration_ms": record.duration_ms,
        "raw_request_text": record.raw_request_text,
        "raw_response_text": record.raw_response_text,
        "request_body_redacted": record.request_body_redacted,
        "response_body_redacted": record.response_body_redacted,
        "upstream_attempts": [_upstream_attempt_to_dict(item) for item in record.upstream_attempts],
        "truncated": record.truncated,
        "request_body_json": parse_json_or_none(record.request_body_text),
        "response_body_json": parse_json_or_none(record.response_body_text),
    }


def parse_json_or_none(value: str | None) -> Any | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
