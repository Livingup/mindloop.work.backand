# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.llm_http_trace.models import LlmHttpTraceRecord

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
    raw_response_text TEXT
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
                       completed_at, duration_ms, raw_request_text, raw_response_text
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
                    completed_at, duration_ms, raw_request_text, raw_response_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    raw_response_text = excluded.raw_response_text
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
        self._conn.commit()
        self._db_path = db_path
        return self._conn


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
    )


def _datetime_to_text(value: datetime) -> str:
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
