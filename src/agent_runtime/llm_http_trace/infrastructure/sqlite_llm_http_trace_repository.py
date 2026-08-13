# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.llm_http_trace.models import (
    LlmHttpTraceLifecycleEvent,
    LlmHttpTraceRecord,
    LlmHttpTraceUpstreamAttempt,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS llm_http_traces (
    llm_request_id TEXT PRIMARY KEY,
    llm_call_id TEXT NOT NULL,
    conversation_id TEXT,
    task_id TEXT,
    instance_id TEXT,
    trace_id TEXT,
    provider_id TEXT,
    binding_id TEXT,
    model_id TEXT,
    model_alias TEXT,
    api_format TEXT,
    candidate_index INTEGER,
    api_key_index INTEGER,
    retry_index INTEGER,
    attempt_count INTEGER,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    request_headers TEXT NOT NULL,
    request_body_text TEXT NOT NULL,
    request_body_bytes INTEGER NOT NULL,
    request_body_truncated INTEGER NOT NULL,
    status_code INTEGER,
    response_headers TEXT NOT NULL,
    response_body_text TEXT,
    response_body_bytes INTEGER NOT NULL,
    response_body_truncated INTEGER NOT NULL,
    response_content_type TEXT,
    is_stream INTEGER NOT NULL,
    sse_event_count INTEGER,
    error_type TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    raw_request_text TEXT,
    raw_response_text TEXT,
    request_body_redacted INTEGER NOT NULL DEFAULT 0,
    response_body_redacted INTEGER NOT NULL DEFAULT 0,
    upstream_attempts TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_llm_http_traces_call ON llm_http_traces(llm_call_id);
CREATE INDEX IF NOT EXISTS idx_llm_http_traces_started ON llm_http_traces(started_at);
CREATE INDEX IF NOT EXISTS idx_llm_http_traces_status ON llm_http_traces(status);
"""


class SqliteLlmHttpTraceRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._db_path = None

    def save_started(self, record: LlmHttpTraceRecord) -> None:
        self._upsert(record)

    def save_completed(self, record: LlmHttpTraceRecord) -> None:
        self._upsert(record)

    def save_failed(self, record: LlmHttpTraceRecord) -> None:
        self._upsert(record)

    def get(self, llm_request_id: str) -> LlmHttpTraceRecord | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT llm_request_id, llm_call_id, conversation_id, task_id, instance_id,
                       trace_id, provider_id, binding_id, model_id, model_alias, api_format,
                       candidate_index, api_key_index, retry_index, attempt_count, status,
                       method, url, request_headers, request_body_text, request_body_bytes,
                       request_body_truncated, status_code, response_headers, response_body_text,
                       response_body_bytes, response_body_truncated, response_content_type,
                       is_stream, sse_event_count, error_type, error_message, started_at,
                       completed_at, duration_ms, raw_request_text, raw_response_text,
                       request_body_redacted, response_body_redacted, upstream_attempts
                FROM llm_http_traces
                WHERE llm_request_id = ?
                """,
                (llm_request_id,),
            ).fetchone()
            return _row_to_record(row) if row else None

    def _upsert(self, record: LlmHttpTraceRecord) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO llm_http_traces (
                    llm_request_id, llm_call_id, conversation_id, task_id, instance_id,
                    trace_id, provider_id, binding_id, model_id, model_alias, api_format,
                    candidate_index, api_key_index, retry_index, attempt_count, status,
                    method, url, request_headers, request_body_text, request_body_bytes,
                    request_body_truncated, status_code, response_headers, response_body_text,
                    response_body_bytes, response_body_truncated, response_content_type,
                    is_stream, sse_event_count, error_type, error_message, started_at,
                    completed_at, duration_ms, raw_request_text, raw_response_text,
                    request_body_redacted, response_body_redacted, upstream_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(llm_request_id) DO UPDATE SET
                    llm_call_id = excluded.llm_call_id,
                    conversation_id = excluded.conversation_id,
                    task_id = excluded.task_id,
                    instance_id = excluded.instance_id,
                    trace_id = excluded.trace_id,
                    provider_id = excluded.provider_id,
                    binding_id = excluded.binding_id,
                    model_id = excluded.model_id,
                    model_alias = excluded.model_alias,
                    api_format = excluded.api_format,
                    candidate_index = excluded.candidate_index,
                    api_key_index = excluded.api_key_index,
                    retry_index = excluded.retry_index,
                    attempt_count = excluded.attempt_count,
                    status = excluded.status,
                    method = excluded.method,
                    url = excluded.url,
                    request_headers = excluded.request_headers,
                    request_body_text = excluded.request_body_text,
                    request_body_bytes = excluded.request_body_bytes,
                    request_body_truncated = excluded.request_body_truncated,
                    status_code = excluded.status_code,
                    response_headers = excluded.response_headers,
                    response_body_text = excluded.response_body_text,
                    response_body_bytes = excluded.response_body_bytes,
                    response_body_truncated = excluded.response_body_truncated,
                    response_content_type = excluded.response_content_type,
                    is_stream = excluded.is_stream,
                    sse_event_count = excluded.sse_event_count,
                    error_type = excluded.error_type,
                    error_message = excluded.error_message,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    duration_ms = excluded.duration_ms,
                    raw_request_text = excluded.raw_request_text,
                    raw_response_text = excluded.raw_response_text,
                    request_body_redacted = excluded.request_body_redacted,
                    response_body_redacted = excluded.response_body_redacted,
                    upstream_attempts = excluded.upstream_attempts
                """,
                _record_to_row(record),
            )
            conn.commit()

    def _connection(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "llm_http_traces.db"
        if self._conn is not None and self._db_path == db_path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        _ensure_schema_columns(self._conn)
        self._conn.commit()
        self._db_path = db_path
        return self._conn


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(llm_http_traces)")}
    additions = {
        "request_body_redacted": "INTEGER NOT NULL DEFAULT 0",
        "response_body_redacted": "INTEGER NOT NULL DEFAULT 0",
        "upstream_attempts": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, declaration in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE llm_http_traces ADD COLUMN {name} {declaration}")


def _attempts_to_json(attempts: tuple[LlmHttpTraceUpstreamAttempt, ...]) -> str:
    return json.dumps([_attempt_to_dict(item) for item in attempts], ensure_ascii=False)


def _attempts_from_json(value: Any) -> tuple[LlmHttpTraceUpstreamAttempt, ...]:
    if not isinstance(value, str) or not value:
        return ()
    try:
        items = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(items, list):
        return ()
    result: list[LlmHttpTraceUpstreamAttempt] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            result.append(_attempt_from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


def _attempt_to_dict(attempt: LlmHttpTraceUpstreamAttempt) -> dict[str, Any]:
    return {
        "sdk_request_id": attempt.sdk_request_id,
        "attempt_id": attempt.attempt_id,
        "attempt_index": attempt.attempt_index,
        "attempt_kind": attempt.attempt_kind,
        "account_fingerprint": attempt.account_fingerprint,
        "status": attempt.status,
        "method": attempt.method,
        "url": attempt.url,
        "request_headers": attempt.request_headers,
        "request_body_text": attempt.request_body_text,
        "request_body_bytes": attempt.request_body_bytes,
        "request_body_truncated": attempt.request_body_truncated,
        "request_body_redacted": attempt.request_body_redacted,
        "status_code": attempt.status_code,
        "response_headers": attempt.response_headers,
        "response_body_text": attempt.response_body_text,
        "response_body_bytes": attempt.response_body_bytes,
        "response_body_truncated": attempt.response_body_truncated,
        "response_body_redacted": attempt.response_body_redacted,
        "response_content_type": attempt.response_content_type,
        "is_stream": attempt.is_stream,
        "sse_event_count": attempt.sse_event_count,
        "error_type": attempt.error_type,
        "error_message": attempt.error_message,
        "started_at": _datetime_to_text(attempt.started_at),
        "completed_at": _datetime_to_text(attempt.completed_at) if attempt.completed_at is not None else None,
        "duration_ms": attempt.duration_ms,
        "raw_request_text": attempt.raw_request_text,
        "raw_response_text": attempt.raw_response_text,
        "lifecycle_events": [_lifecycle_to_dict(item) for item in attempt.lifecycle_events],
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
        "terminal_error": attempt.terminal_error,
        "callback_events": list(attempt.callback_events),
        "capture_complete": attempt.capture_complete,
        "capture_error": attempt.capture_error,
        "capture_source": attempt.capture_source,
        "socket_level_wire": attempt.socket_level_wire,
    }


def _attempt_from_dict(value: dict[str, Any]) -> LlmHttpTraceUpstreamAttempt:
    lifecycle = value.get("lifecycle_events")
    return LlmHttpTraceUpstreamAttempt(
        sdk_request_id=str(value["sdk_request_id"]),
        attempt_id=str(value["attempt_id"]),
        attempt_index=int(value["attempt_index"]),
        attempt_kind=str(value["attempt_kind"]),
        account_fingerprint=_optional_text(value.get("account_fingerprint")),
        status=str(value["status"]),
        method=str(value["method"]),
        url=str(value["url"]),
        request_headers=_string_dict(value.get("request_headers")),
        request_body_text=str(value.get("request_body_text") or ""),
        request_body_bytes=int(value.get("request_body_bytes") or 0),
        request_body_truncated=bool(value.get("request_body_truncated")),
        request_body_redacted=bool(value.get("request_body_redacted")),
        status_code=int(value["status_code"]) if value.get("status_code") is not None else None,
        response_headers=_string_dict(value.get("response_headers")),
        response_body_text=_optional_text(value.get("response_body_text")),
        response_body_bytes=int(value.get("response_body_bytes") or 0),
        response_body_truncated=bool(value.get("response_body_truncated")),
        response_body_redacted=bool(value.get("response_body_redacted")),
        response_content_type=_optional_text(value.get("response_content_type")),
        is_stream=bool(value.get("is_stream")),
        sse_event_count=int(value.get("sse_event_count") or 0),
        error_type=_optional_text(value.get("error_type")),
        error_message=_optional_text(value.get("error_message")),
        started_at=_text_to_datetime(str(value["started_at"])),
        completed_at=(
            _text_to_datetime(str(value["completed_at"]))
            if value.get("completed_at") is not None
            else None
        ),
        duration_ms=int(value["duration_ms"]) if value.get("duration_ms") is not None else None,
        raw_request_text=_optional_text(value.get("raw_request_text")),
        raw_response_text=_optional_text(value.get("raw_response_text")),
        lifecycle_events=tuple(
            _lifecycle_from_dict(item)
            for item in lifecycle or ()
            if isinstance(item, dict)
        ),
        lifecycle_event_count=int(value.get("lifecycle_event_count") or 0),
        lifecycle_truncated=bool(value.get("lifecycle_truncated")),
        account_id=_optional_text(value.get("account_id")),
        request_header_pairs=_header_pairs(value.get("request_header_pairs")),
        request_body_base64=_optional_text(value.get("request_body_base64")),
        response_header_pairs=_header_pairs(value.get("response_header_pairs")),
        response_body_chunks_base64=_string_tuple(value.get("response_body_chunks_base64")),
        sse_frames_base64=_string_tuple(value.get("sse_frames_base64")),
        sse_data_base64=_optional_string_tuple(value.get("sse_data_base64")),
        sse_frame_bytes=int(value.get("sse_frame_bytes") or 0),
        sse_data_bytes=int(value.get("sse_data_bytes") or 0),
        terminal_error=value.get("terminal_error") if isinstance(value.get("terminal_error"), dict) else None,
        callback_events=tuple(item for item in (value.get("callback_events") or ()) if isinstance(item, dict)),
        capture_complete=bool(value.get("capture_complete")),
        capture_error=_optional_text(value.get("capture_error")),
        capture_source=str(value.get("capture_source") or "unknown"),
        socket_level_wire=(
            bool(value["socket_level_wire"])
            if value.get("socket_level_wire") is not None
            else None
        ),
    )


def _lifecycle_to_dict(event: LlmHttpTraceLifecycleEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "kind": event.kind,
        "timestamp": _datetime_to_text(event.timestamp),
        "redacted": event.redacted,
        "byte_count": event.byte_count,
        "data_byte_count": event.data_byte_count,
        "status": event.status,
        "error_code": event.error_code,
    }


def _lifecycle_from_dict(value: dict[str, Any]) -> LlmHttpTraceLifecycleEvent:
    return LlmHttpTraceLifecycleEvent(
        sequence=int(value["sequence"]),
        kind=str(value["kind"]),
        timestamp=_text_to_datetime(str(value["timestamp"])),
        redacted=bool(value.get("redacted")),
        byte_count=int(value["byte_count"]) if value.get("byte_count") is not None else None,
        data_byte_count=(
            int(value["data_byte_count"])
            if value.get("data_byte_count") is not None
            else None
        ),
        status=_optional_text(value.get("status")),
        error_code=_optional_text(value.get("error_code")),
    )


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _header_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(item[0]), str(item[1]))
        for item in (value or ())
        if isinstance(item, (list, tuple)) and len(item) >= 2
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or ()) if item is not None)


def _optional_string_tuple(value: Any) -> tuple[str | None, ...]:
    return tuple(None if item is None else str(item) for item in (value or ()))


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _record_to_row(record: LlmHttpTraceRecord) -> tuple[Any, ...]:
    return (
        record.llm_request_id,
        record.llm_call_id,
        record.conversation_id,
        record.task_id,
        record.instance_id,
        record.trace_id,
        record.provider_id,
        record.binding_id,
        record.model_id,
        record.model_alias,
        record.api_format,
        record.candidate_index,
        record.api_key_index,
        record.retry_index,
        record.attempt_count,
        record.status,
        record.method,
        record.url,
        json.dumps(record.request_headers, ensure_ascii=False),
        record.request_body_text,
        record.request_body_bytes,
        1 if record.request_body_truncated else 0,
        record.status_code,
        json.dumps(record.response_headers, ensure_ascii=False),
        record.response_body_text,
        record.response_body_bytes,
        1 if record.response_body_truncated else 0,
        record.response_content_type,
        1 if record.is_stream else 0,
        record.sse_event_count,
        record.error_type,
        record.error_message,
        _datetime_to_text(record.started_at),
        _datetime_to_text(record.completed_at) if record.completed_at is not None else None,
        record.duration_ms,
        record.raw_request_text,
        record.raw_response_text,
        1 if record.request_body_redacted else 0,
        1 if record.response_body_redacted else 0,
        _attempts_to_json(record.upstream_attempts),
    )


def _row_to_record(row: tuple[Any, ...]) -> LlmHttpTraceRecord:
    return LlmHttpTraceRecord(
        llm_request_id=row[0],
        llm_call_id=row[1],
        conversation_id=row[2],
        task_id=row[3],
        instance_id=row[4],
        trace_id=row[5],
        provider_id=row[6],
        binding_id=row[7],
        model_id=row[8],
        model_alias=row[9],
        api_format=row[10],
        candidate_index=row[11],
        api_key_index=row[12],
        retry_index=row[13],
        attempt_count=row[14],
        status=row[15],
        method=row[16],
        url=row[17],
        request_headers=json.loads(row[18]),
        request_body_text=row[19],
        request_body_bytes=int(row[20]),
        request_body_truncated=bool(row[21]),
        status_code=row[22],
        response_headers=json.loads(row[23]),
        response_body_text=row[24],
        response_body_bytes=int(row[25]),
        response_body_truncated=bool(row[26]),
        response_content_type=row[27],
        is_stream=bool(row[28]),
        sse_event_count=row[29],
        error_type=row[30],
        error_message=row[31],
        started_at=_text_to_datetime(row[32]),
        completed_at=_text_to_datetime(row[33]) if row[33] is not None else None,
        duration_ms=row[34],
        raw_request_text=row[35],
        raw_response_text=row[36],
        request_body_redacted=bool(row[37]),
        response_body_redacted=bool(row[38]),
        upstream_attempts=_attempts_from_json(row[39]),
    )


def _datetime_to_text(value: datetime) -> str:
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
