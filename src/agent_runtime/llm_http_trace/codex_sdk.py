# -*- coding: utf-8 -*-
"""Request-scoped lossless bridge from louyi SDK raw events to HTTP trace storage."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_runtime.codex_oauth.diagnostics import CodexSdkRawEventSink
from agent_runtime.llm_http_trace.models import (
    LLM_HTTP_TRACE_STATUS_COMPLETED,
    LLM_HTTP_TRACE_STATUS_FAILED,
    LLM_HTTP_TRACE_STATUS_STARTED,
    LlmHttpTraceContext,
    LlmHttpTraceLifecycleEvent,
    LlmHttpTraceUpstreamAttempt,
)
from agent_runtime.llm_http_trace.service import LlmHttpTraceService

logger = logging.getLogger("agent_runtime.codex_sdk")


class CodexSdkHttpTraceSink:
    """Capture UPSTREAM_ATTEMPT callbacks in memory and persist once at the SDK request boundary."""

    def __init__(self, *, service: LlmHttpTraceService, context: LlmHttpTraceContext) -> None:
        self._service = service
        self._context = context
        self._diagnostic_sink = CodexSdkRawEventSink()
        self._attempts: list[_AttemptState] = []
        self._attempts_by_id: dict[str, _AttemptState] = {}
        self._last_account_id: str | None = None
        self._flushed = False
        self._flush_result = False

    def emit(self, event: Any) -> None:
        try:
            self._diagnostic_sink.emit(event)
        except Exception:
            logger.exception("codex_sdk.diagnostic_sink_failed")
        try:
            self._emit_trace(event)
        except Exception as exc:
            logger.exception("codex_sdk.http_trace_capture_failed")
            self._mark_capture_failure(event, exc)

    def _emit_trace(self, event: Any) -> None:
        scope = str(_enum_value(getattr(event, "scope")))
        if scope != "upstream_attempt":
            return
        kind = str(_enum_value(getattr(event, "kind")))
        if kind not in {"request_prepared", "request_body", "response_headers", "response_body_chunk", "sse_frame", "parsed_event", "terminal"}:
            return
        raw_attempt_id = getattr(event, "attempt_id")
        if raw_attempt_id is None:
            return
        attempt_id = str(raw_attempt_id)
        payload = getattr(event, "payload")
        timestamp = _event_timestamp(event)
        sequence = int(getattr(event, "sequence"))
        state = self._attempts_by_id.get(attempt_id)

        if kind == "request_prepared":
            state = self._start_attempt(event, payload, attempt_id, timestamp)
        elif state is None:
            return

        expected_sequence = state.callback_events[-1]["sequence"] + 1 if state.callback_events else 1
        if sequence != expected_sequence and state.capture_error is None:
            state.capture_error = f"callback sequence discontinuity: expected {expected_sequence}, received {sequence}"
        callback_payload: dict[str, Any]
        lifecycle = LlmHttpTraceLifecycleEvent(sequence=sequence, kind=kind, timestamp=timestamp)
        if kind == "request_prepared":
            callback_payload = {
                "method": state.method,
                "url": state.url,
                "headers": [list(item) for item in state.request_header_pairs],
            }
        elif kind == "request_body":
            body = _coerce_bytes(getattr(payload, "body"))
            state.request_body = body
            state.request_body_seen = True
            state.request_body_bytes = len(body)
            callback_payload = _bytes_payload("body", body)
            lifecycle = LlmHttpTraceLifecycleEvent(sequence=sequence, kind=kind, timestamp=timestamp, byte_count=len(body))
        elif kind == "response_headers":
            state.response_headers_seen = True
            state.status_code = int(getattr(payload, "status_code"))
            state.response_header_pairs = _header_pairs(getattr(payload, "headers"))
            state.response_headers = _headers_to_dict(state.response_header_pairs)
            callback_payload = {
                "status_code": state.status_code,
                "headers": [list(item) for item in state.response_header_pairs],
            }
        elif kind == "response_body_chunk":
            chunk = _coerce_bytes(getattr(payload, "chunk"))
            state.response_chunks.append(chunk)
            state.response_body_bytes += len(chunk)
            callback_payload = _bytes_payload("chunk", chunk)
            lifecycle = LlmHttpTraceLifecycleEvent(sequence=sequence, kind=kind, timestamp=timestamp, byte_count=len(chunk))
        elif kind == "sse_frame":
            frame = _coerce_bytes(getattr(payload, "frame"))
            raw_data = getattr(payload, "data")
            data = None if raw_data is None else _coerce_bytes(raw_data)
            state.sse_frames.append((frame, data))
            state.sse_event_count += 1
            state.sse_frame_bytes += len(frame)
            state.sse_data_bytes += 0 if data is None else len(data)
            callback_payload = _bytes_payload("frame", frame)
            callback_payload["data"] = None if data is None else _encoded_bytes(data)
            lifecycle = LlmHttpTraceLifecycleEvent(
                sequence=sequence, kind=kind, timestamp=timestamp, byte_count=len(frame),
                data_byte_count=None if data is None else len(data),
            )
        elif kind == "parsed_event":
            callback_payload = {"event": _parsed_event_to_json(getattr(payload, "event"))}
        else:
            terminal_status = str(_enum_value(getattr(payload, "status")))
            terminal_error = _terminal_error_to_dict(getattr(payload, "error"))
            state.terminal_status = terminal_status
            state.terminal_error = terminal_error
            state.completed_at = timestamp
            state.error_type = None if terminal_error is None else _optional_string(terminal_error.get("code"))
            state.error_message = None if terminal_error is None else _optional_string(terminal_error.get("message"))
            callback_payload = {"status": terminal_status, "error": terminal_error}
            lifecycle = LlmHttpTraceLifecycleEvent(
                sequence=sequence, kind=kind, timestamp=timestamp, status=terminal_status,
                error_code=state.error_type,
            )

        state.lifecycle_events.append(lifecycle)
        state.callback_events.append({
            "request_id": str(getattr(event, "request_id")),
            "attempt_id": str(getattr(event, "attempt_id")),
            "account_id": _optional_string(getattr(event, "account_id")),
            "scope": str(_enum_value(getattr(event, "scope"))),
            "origin": _optional_string(_enum_value(getattr(event, "origin"))),
            "kind": str(_enum_value(getattr(event, "kind"))),
            "sequence": int(getattr(event, "sequence")),
            "timestamp": _event_timestamp(event).isoformat(),
            "payload": callback_payload,
        })

    def _start_attempt(self, event: Any, payload: Any, attempt_id: str, timestamp: datetime) -> "_AttemptState":
        account_id = _optional_string(getattr(event, "account_id"))
        attempt_kind = "initial" if not self._attempts else (
            "same_account_retry" if account_id and account_id == self._last_account_id else "failover"
        )
        self._last_account_id = account_id or self._last_account_id
        pairs = _header_pairs(getattr(payload, "headers"))
        state = _AttemptState(
            sdk_request_id=str(getattr(event, "request_id")),
            attempt_id=attempt_id,
            account_id=account_id,
            attempt_index=len(self._attempts) + 1,
            attempt_kind=attempt_kind,
            method=str(getattr(payload, "method")),
            url=str(getattr(payload, "url")),
            request_header_pairs=pairs,
            request_headers=_headers_to_dict(pairs),
            started_at=timestamp,
        )
        self._attempts.append(state)
        self._attempts_by_id[attempt_id] = state
        return state

    def _mark_capture_failure(self, event: Any, exc: Exception) -> None:
        try:
            raw_attempt_id = getattr(event, "attempt_id", None)
            state = None if raw_attempt_id is None else self._attempts_by_id.get(str(raw_attempt_id))
            if state is None:
                return
            if state.capture_error is None:
                kind = str(_enum_value(getattr(event, "kind", "unknown")))
                state.capture_error = f"callback capture failed for {kind}: {exc.__class__.__name__}"
            logger.error(
                "codex_sdk.http_trace_capture_failure request_id=%s attempt_id=%s kind=%s",
                getattr(event, "request_id", None),
                raw_attempt_id,
                getattr(event, "kind", "unknown"),
            )
        except Exception:
            logger.exception("codex_sdk.http_trace_capture_failure_marker_failed")

    def flush(self) -> bool:
        """Persist all real SDK upstream attempts once at the SDK request boundary."""
        if self._flushed:
            return self._flush_result
        self._flushed = True
        if not self._attempts:
            return False
        try:
            persisted = self._service.record_upstream_attempts(
                context=self._context,
                attempts=tuple(item.freeze() for item in self._attempts),
            )
        except Exception:
            logger.exception(
                "codex_sdk.http_trace_final_flush_failed request_id=%s",
                self._context.llm_request_id,
            )
            persisted = False
        if not persisted:
            active = self._attempts[-1]
            if active.capture_error is None:
                active.capture_error = "callback snapshot persistence failed"
            logger.error(
                "codex_sdk.http_trace_final_flush_not_persisted request_id=%s attempts=%d",
                self._context.llm_request_id,
                len(self._attempts),
            )
        self._flush_result = persisted
        return persisted


@dataclass(slots=True)
class _AttemptState:
    sdk_request_id: str
    attempt_id: str
    account_id: str | None
    attempt_index: int
    attempt_kind: str
    method: str
    url: str
    request_header_pairs: tuple[tuple[str, str], ...]
    request_headers: dict[str, str]
    started_at: datetime
    request_body: bytes = b""
    request_body_seen: bool = False
    request_body_bytes: int = 0
    response_headers_seen: bool = False
    status_code: int | None = None
    response_header_pairs: tuple[tuple[str, str], ...] = ()
    response_headers: dict[str, str] = field(default_factory=dict)
    response_chunks: list[bytes] = field(default_factory=list)
    response_body_bytes: int = 0
    sse_frames: list[tuple[bytes, bytes | None]] = field(default_factory=list)
    sse_event_count: int = 0
    sse_frame_bytes: int = 0
    sse_data_bytes: int = 0
    terminal_status: str | None = None
    terminal_error: dict[str, Any] | None = None
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    lifecycle_events: list[LlmHttpTraceLifecycleEvent] = field(default_factory=list)
    callback_events: list[dict[str, Any]] = field(default_factory=list)
    capture_error: str | None = None

    def freeze(self) -> LlmHttpTraceUpstreamAttempt:
        status = (
            LLM_HTTP_TRACE_STATUS_COMPLETED if self.terminal_status == "completed" else
            LLM_HTTP_TRACE_STATUS_FAILED if self.terminal_status is not None else
            LLM_HTTP_TRACE_STATUS_STARTED
        )
        duration_ms = None if self.completed_at is None else max(
            0, int((self.completed_at - self.started_at).total_seconds() * 1000)
        )
        response_body = b"".join(self.response_chunks)
        response_content_type = _header_value(self.response_header_pairs, "content-type")
        return LlmHttpTraceUpstreamAttempt(
            sdk_request_id=self.sdk_request_id, attempt_id=self.attempt_id,
            attempt_index=self.attempt_index, attempt_kind=self.attempt_kind,
            account_fingerprint=None, status=status, method=self.method, url=self.url,
            request_headers=dict(self.request_headers),
            request_body_text=_display_bytes(self.request_body) if self.request_body_seen else "",
            request_body_bytes=self.request_body_bytes, request_body_truncated=False,
            request_body_redacted=False, status_code=self.status_code,
            response_headers=dict(self.response_headers),
            response_body_text=_display_bytes(response_body) if self.response_chunks else None,
            response_body_bytes=self.response_body_bytes, response_body_truncated=False,
            response_body_redacted=False, response_content_type=response_content_type,
            is_stream=True, sse_event_count=self.sse_event_count,
            error_type=self.error_type, error_message=self.error_message,
            started_at=self.started_at, completed_at=self.completed_at, duration_ms=duration_ms,
            raw_request_text=_render_request_snapshot(self),
            raw_response_text=_render_response_snapshot(self),
            lifecycle_events=tuple(self.lifecycle_events),
            lifecycle_event_count=len(self.lifecycle_events), lifecycle_truncated=False,
            account_id=self.account_id, request_header_pairs=self.request_header_pairs,
            request_body_base64=base64.b64encode(self.request_body).decode("ascii") if self.request_body_seen else None,
            response_header_pairs=self.response_header_pairs,
            response_body_chunks_base64=tuple(base64.b64encode(item).decode("ascii") for item in self.response_chunks),
            sse_frames_base64=tuple(base64.b64encode(item[0]).decode("ascii") for item in self.sse_frames),
            sse_data_base64=tuple(None if item[1] is None else base64.b64encode(item[1]).decode("ascii") for item in self.sse_frames),
            sse_frame_bytes=self.sse_frame_bytes, sse_data_bytes=self.sse_data_bytes,
            terminal_error=self.terminal_error, callback_events=tuple(self.callback_events),
            capture_complete=self.terminal_status is not None and self.capture_error is None,
            capture_error=self.capture_error,
            capture_source="louyi_sdk_upstream_attempt_callback",
            socket_level_wire=False,
        )


def _render_request_snapshot(state: _AttemptState) -> str:
    lines = _snapshot_preamble(state, "REQUEST CALLBACKS")
    for event in state.callback_events:
        if event["kind"] not in {"request_prepared", "request_body"}:
            continue
        lines.extend(_render_event(event))
    return "\n".join(lines)


def _render_response_snapshot(state: _AttemptState) -> str:
    lines = _snapshot_preamble(state, "RESPONSE / SSE / TERMINAL CALLBACKS")
    events = [item for item in state.callback_events if item["kind"] not in {"request_prepared", "request_body"}]
    if not events:
        lines.append("[no response-side SDK callback received]")
    for event in events:
        lines.extend(_render_event(event))
    return "\n".join(lines)


def _snapshot_preamble(state: _AttemptState, section: str) -> list[str]:
    capture_status = (
        "complete" if state.terminal_status is not None and state.capture_error is None else
        "incomplete" if state.capture_error is not None else
        "capturing"
    )
    lines = [
        "=== CODEX SDK UPSTREAM_ATTEMPT CALLBACK SNAPSHOT ===",
        "Source: RequestOptions.raw_event_sink (SDK callback values; not socket-level HTTP wire)",
        f"Section: {section}",
        f"SDK request_id: {state.sdk_request_id}",
        f"attempt_id: {state.attempt_id}",
        f"account_id: {state.account_id}",
        f"capture_status: {capture_status}",
    ]
    if state.capture_error is not None:
        lines.append(f"capture_error: {state.capture_error}")
    lines.append("")
    return lines


def _render_event(event: dict[str, Any]) -> list[str]:
    payload = event["payload"]
    lines = [
        "--- SDK CALLBACK EVENT ---",
        f"scope: {event['scope']}",
        f"origin: {event['origin']}",
        f"request_id: {event['request_id']}",
        f"attempt_id: {event['attempt_id']}",
        f"account_id: {event['account_id']}",
        f"sequence: {event['sequence']}",
        f"timestamp: {event['timestamp']}",
        f"kind: {event['kind']}",
        "payload:",
    ]
    kind = event["kind"]
    if kind == "request_prepared":
        lines.extend([f"method: {payload['method']}", f"url: {payload['url']}", "headers (ordered pairs; duplicates preserved):"])
        lines.extend(f"  [{index}] {name}: {value}" for index, (name, value) in enumerate(payload["headers"]))
    elif kind == "response_headers":
        lines.extend([f"status_code: {payload['status_code']}", "headers (ordered pairs; duplicates preserved):"])
        lines.extend(f"  [{index}] {name}: {value}" for index, (name, value) in enumerate(payload["headers"]))
    elif kind in {"request_body", "response_body_chunk"}:
        key = "body" if kind == "request_body" else "chunk"
        lines.extend(_render_encoded_bytes(key, payload[key]))
    elif kind == "sse_frame":
        lines.extend(_render_encoded_bytes("frame", payload["frame"]))
        lines.append("data: <not provided by callback>" if payload["data"] is None else "data:")
        if payload["data"] is not None:
            lines.extend(_render_encoded_bytes("data", payload["data"]))
    elif kind == "parsed_event":
        lines.append(json.dumps(payload["event"], ensure_ascii=False, indent=2))
    else:
        lines.extend([f"status: {payload['status']}", f"error: {payload['error']!r}"])
    lines.append("")
    return lines


def _render_encoded_bytes(label: str, encoded: dict[str, Any]) -> list[str]:
    if encoded["encoding"] == "utf-8":
        return [f"{label}_bytes: {encoded['byte_count']}", f"{label}_encoding: utf-8", f"{label}:", encoded["text"]]
    return [f"{label}_bytes: {encoded['byte_count']}", f"{label}_encoding: base64", f"{label}_base64:", encoded["base64"]]


def _bytes_payload(name: str, value: bytes) -> dict[str, Any]:
    return {name: _encoded_bytes(value)}


def _encoded_bytes(value: bytes) -> dict[str, Any]:
    try:
        return {"encoding": "utf-8", "byte_count": len(value), "text": value.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "byte_count": len(value), "base64": base64.b64encode(value).decode("ascii")}


def _display_bytes(value: bytes) -> str:
    encoded = _encoded_bytes(value)
    return encoded.get("text") if encoded["encoding"] == "utf-8" else f"[base64:{encoded['base64']}]"


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError("callback byte payload is not bytes-like")


def _header_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError("callback header entry is not a name/value pair")
        pairs.append((str(item[0]), str(item[1])))
    return tuple(pairs)


def _headers_to_dict(value: tuple[tuple[str, str], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, item in value:
        result[name] = f"{result[name]}, {item}" if name in result else item
    return result


def _header_value(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    return next((value for key, value in headers if key.lower() == name.lower()), None)


def _terminal_error_to_dict(error: Any) -> dict[str, Any] | None:
    if error is None:
        return None
    retry_at = getattr(error, "retry_at", None)
    return {
        "code": getattr(error, "code", None),
        "message": getattr(error, "message", None),
        "retryable": getattr(error, "retryable", None),
        "http_status": getattr(error, "http_status", None),
        "retry_at": retry_at.isoformat() if isinstance(retry_at, datetime) else retry_at,
    }


def _parsed_event_to_json(event: Any) -> Any:
    model_dump = getattr(event, "model_dump", None)
    if not callable(model_dump):
        raise TypeError("parsed_event payload does not expose model_dump")
    value = model_dump(mode="json")
    json.dumps(value, ensure_ascii=False)
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _event_timestamp(event: Any) -> datetime:
    value = getattr(event, "timestamp")
    if not isinstance(value, datetime):
        raise TypeError("callback timestamp is not a datetime")
    return value
