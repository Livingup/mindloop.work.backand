from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.llm_http_trace.config import LlmHttpTraceConfig
from agent_runtime.llm_http_trace.infrastructure.sqlite_llm_http_trace_repository import SqliteLlmHttpTraceRepository
from agent_runtime.llm_http_trace.models import LlmHttpTraceRecord
from agent_runtime.llm_http_trace.north_gateway.llm_http_trace_rest_adapter import create_llm_http_trace_router
from agent_runtime.llm_http_trace.service import LlmHttpTraceService
from agent_runtime.logs_management.north_gateway.logs_management_rest_adapter import create_logs_management_router
from agent_runtime.logs_management.service import (
    CATEGORY_CHAT_BUBBLES,
    CATEGORY_COMMUNICATION_MESSAGES,
    CATEGORY_CONTEXT_COMPRESSION_ARCHIVE,
    CATEGORY_LLM_HTTP_TRACE,
    CATEGORY_TERMINAL_HISTORY,
    LIFECYCLE_ARCHIVED_SCENES,
    LIFECYCLE_ENDED_TASKS,
    LogsManagementService,
    LogsStorePaths,
 )


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path))


def _paths(tmp_path: Path) -> LogsStorePaths:
    return LogsStorePaths(
        agent_runtime_db=tmp_path / "agent-runtime" / "agent_runtime.db",
        scene_db=tmp_path / "scene" / "scene.db",
        communication_db=tmp_path / "communication" / "communication.db",
        chat_bubbles_db=tmp_path / "agent-runtime" / "chat_bubbles.db",
        llm_http_trace_db=tmp_path / "llm-http-traces" / "llm_http_traces.db",
        context_compression_dir=tmp_path / "context-compression",
        context_compression_db=tmp_path / "context-compression" / "context_compression.db",
        terminal_db=tmp_path / "terminal" / "terminal.db",
    )


def _seed_lifecycle(paths: LogsStorePaths) -> None:
    with _connect(paths.agent_runtime_db) as conn:
        conn.execute("CREATE TABLE agent_tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, scene_id TEXT)")
        conn.executemany(
            "INSERT INTO agent_tasks (task_id, status, scene_id) VALUES (?, ?, ?)",
            [
                ("task-completed", "completed", "scene-active"),
                ("task-cancelled", "cancelled", "scene-active"),
                ("task-active", "unfinished", "scene-active"),
                ("task-archived-scene", "unfinished", "scene-archived"),
            ],
        )
    with _connect(paths.scene_db) as conn:
        conn.execute("CREATE TABLE scenes (scene_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO scenes (scene_id, status) VALUES (?, ?)",
            [("scene-active", "active"), ("scene-archived", "archived")],
        )


def _seed_logs(paths: LogsStorePaths) -> None:
    with _connect(paths.chat_bubbles_db) as conn:
        conn.execute("CREATE TABLE chat_bubble_messages (bubble_id TEXT PRIMARY KEY, task_id TEXT, scene_id TEXT)")
        conn.executemany(
            "INSERT INTO chat_bubble_messages (bubble_id, task_id, scene_id) VALUES (?, ?, ?)",
            [
                ("bubble-ended", "task-completed", "scene-active"),
                ("bubble-cancelled", "task-cancelled", "scene-active"),
                ("bubble-active", "task-active", "scene-active"),
                ("bubble-archived-scene", None, "scene-archived"),
            ],
        )
    with _connect(paths.communication_db) as conn:
        conn.execute("CREATE TABLE communication_messages (message_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, metadata TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO communication_messages (message_id, stream_id, metadata) VALUES (?, ?, ?)",
            [
                ("message-ended", "stream-scene-scene-active", '{"task_id":"task-completed"}'),
                ("message-cancelled", "stream-scene-scene-active", '{"task_id":"task-cancelled"}'),
                ("message-active", "stream-scene-scene-active", '{"task_id":"task-active"}'),
                ("message-archived-scene", "stream-scene-scene-archived", '{}'),
            ],
        )
    with _connect(paths.llm_http_trace_db) as conn:
        conn.execute("CREATE TABLE llm_http_traces (llm_request_id TEXT PRIMARY KEY, task_id TEXT)")
        conn.executemany(
            "INSERT INTO llm_http_traces (llm_request_id, task_id) VALUES (?, ?)",
            [
                ("trace-ended", "task-completed"),
                ("trace-cancelled", "task-cancelled"),
                ("trace-active", "task-active"),
                ("trace-archived-scene", "task-archived-scene"),
                ("trace-unlinked", None),
            ],
        )
    paths.context_compression_dir.mkdir(parents=True, exist_ok=True)
    summaries = paths.context_compression_dir / "summaries"
    snapshots = paths.context_compression_dir / "snapshots"
    summaries.mkdir(parents=True, exist_ok=True)
    snapshots.mkdir(parents=True, exist_ok=True)
    summary_ended = summaries / "job-ended.md"
    snapshot_ended = snapshots / "job-ended-before.jsonl"
    summary_active = summaries / "job-active.md"
    summary_ended.write_text("ended", encoding="utf-8")
    snapshot_ended.write_text("snapshot", encoding="utf-8")
    summary_active.write_text("active", encoding="utf-8")
    with _connect(paths.context_compression_db) as conn:
        conn.execute(
            """
            CREATE TABLE context_compression_jobs (
                job_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                final_summary_path TEXT,
                before_messages_snapshot_path TEXT,
                after_messages_snapshot_path TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO context_compression_jobs (job_id, task_id, final_summary_path, before_messages_snapshot_path, after_messages_snapshot_path) VALUES (?, ?, ?, ?, ?)",
            [
                ("job-ended", "task-completed", str(summary_ended), str(snapshot_ended), None),
                ("job-active", "task-active", str(summary_active), None, None),
            ],
        )
    with _connect(paths.terminal_db) as conn:
        conn.execute("CREATE TABLE terminals (terminal_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, metadata_json TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO terminals (terminal_id, owner_type, owner_id, metadata_json) VALUES (?, ?, ?, ?)",
            [
                ("terminal-ended", "task", "task-completed", '{}'),
                ("terminal-active", "task", "task-active", '{}'),
                ("terminal-archived-scene", "scene", "scene-archived", '{}'),
            ],
        )


def _service(paths: LogsStorePaths) -> LogsManagementService:
    return LogsManagementService(paths_accessor=lambda: paths)


def test_manual_cleanup_deletes_only_selected_lifecycle_and_categories(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_lifecycle(paths)
    _seed_logs(paths)
    service = _service(paths)

    request = {
        "lifecycle_filters": [LIFECYCLE_ENDED_TASKS],
        "categories": [
            CATEGORY_COMMUNICATION_MESSAGES,
            CATEGORY_CHAT_BUBBLES,
            CATEGORY_LLM_HTTP_TRACE,
            CATEGORY_CONTEXT_COMPRESSION_ARCHIVE,
            CATEGORY_TERMINAL_HISTORY,
        ],
    }
    preview = service.preview_cleanup(**request)

    assert preview["total_matched_count"] == 8
    assert {item["category"]: item["matched_count"] for item in preview["category_results"]} == {
        CATEGORY_COMMUNICATION_MESSAGES: 2,
        CATEGORY_CHAT_BUBBLES: 2,
        CATEGORY_LLM_HTTP_TRACE: 2,
        CATEGORY_CONTEXT_COMPRESSION_ARCHIVE: 1,
        CATEGORY_TERMINAL_HISTORY: 1,
    }

    result = service.execute_cleanup(**request)
    assert result["total_deleted_count"] == preview["total_matched_count"]

    with _connect(paths.communication_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT message_id FROM communication_messages").fetchall()}
        assert remaining == {"message-active", "message-archived-scene"}
    with _connect(paths.chat_bubbles_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT bubble_id FROM chat_bubble_messages").fetchall()}
        assert remaining == {"bubble-active", "bubble-archived-scene"}
    with _connect(paths.llm_http_trace_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT llm_request_id FROM llm_http_traces").fetchall()}
        assert remaining == {"trace-active", "trace-archived-scene", "trace-unlinked"}
    with _connect(paths.context_compression_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT job_id FROM context_compression_jobs").fetchall()}
        assert remaining == {"job-active"}
    assert not (paths.context_compression_dir / "summaries" / "job-ended.md").exists()
    assert not (paths.context_compression_dir / "snapshots" / "job-ended-before.jsonl").exists()
    assert (paths.context_compression_dir / "summaries" / "job-active.md").exists()
    with _connect(paths.terminal_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT terminal_id FROM terminals").fetchall()}
        assert remaining == {"terminal-active", "terminal-archived-scene"}


def test_archived_scene_cleanup_matches_scene_scope_and_derives_scene_tasks(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_lifecycle(paths)
    _seed_logs(paths)
    service = _service(paths)

    result = service.execute_cleanup(
        lifecycle_filters=[LIFECYCLE_ARCHIVED_SCENES],
        categories=[CATEGORY_COMMUNICATION_MESSAGES, CATEGORY_CHAT_BUBBLES, CATEGORY_LLM_HTTP_TRACE, CATEGORY_TERMINAL_HISTORY],
    )

    assert result["scope"] == {"task_count": 1, "scene_count": 1}
    assert {item["category"]: item["deleted_count"] for item in result["category_results"]} == {
        CATEGORY_COMMUNICATION_MESSAGES: 1,
        CATEGORY_CHAT_BUBBLES: 1,
        CATEGORY_LLM_HTTP_TRACE: 1,
        CATEGORY_TERMINAL_HISTORY: 1,
    }
    with _connect(paths.communication_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT message_id FROM communication_messages").fetchall()}
        assert "message-archived-scene" not in remaining
        assert "message-active" in remaining
    with _connect(paths.llm_http_trace_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT llm_request_id FROM llm_http_traces").fetchall()}
        assert "trace-archived-scene" not in remaining
        assert "trace-unlinked" in remaining


def test_cleanup_with_multiple_lifecycle_filters_uses_lifecycle_intersection(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_lifecycle(paths)
    _seed_logs(paths)

    with _connect(paths.agent_runtime_db) as conn:
        conn.execute(
            "INSERT INTO agent_tasks (task_id, status, scene_id) VALUES (?, ?, ?)",
            ("task-ended-archived-scene", "completed", "scene-archived"),
        )
    with _connect(paths.communication_db) as conn:
        conn.execute(
            "INSERT INTO communication_messages (message_id, stream_id, metadata) VALUES (?, ?, ?)",
            ("message-ended-archived-scene", "stream-scene-scene-archived", '{"task_id":"task-ended-archived-scene"}'),
        )
    with _connect(paths.chat_bubbles_db) as conn:
        conn.execute(
            "INSERT INTO chat_bubble_messages (bubble_id, task_id, scene_id) VALUES (?, ?, ?)",
            ("bubble-ended-archived-scene", "task-ended-archived-scene", "scene-archived"),
        )
    with _connect(paths.llm_http_trace_db) as conn:
        conn.execute(
            "INSERT INTO llm_http_traces (llm_request_id, task_id) VALUES (?, ?)",
            ("trace-ended-archived-scene", "task-ended-archived-scene"),
        )
    with _connect(paths.terminal_db) as conn:
        conn.execute(
            "INSERT INTO terminals (terminal_id, owner_type, owner_id, metadata_json) VALUES (?, ?, ?, ?)",
            ("terminal-ended-archived-scene", "task", "task-ended-archived-scene", '{}'),
        )

    service = _service(paths)
    result = service.execute_cleanup(
        lifecycle_filters=[LIFECYCLE_ENDED_TASKS, LIFECYCLE_ARCHIVED_SCENES],
        categories=[CATEGORY_COMMUNICATION_MESSAGES, CATEGORY_CHAT_BUBBLES, CATEGORY_LLM_HTTP_TRACE, CATEGORY_TERMINAL_HISTORY],
    )

    assert result["scope"] == {"task_count": 1, "scene_count": 0}
    assert {item["category"]: item["deleted_count"] for item in result["category_results"]} == {
        CATEGORY_COMMUNICATION_MESSAGES: 1,
        CATEGORY_CHAT_BUBBLES: 1,
        CATEGORY_LLM_HTTP_TRACE: 1,
        CATEGORY_TERMINAL_HISTORY: 1,
    }
    with _connect(paths.communication_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT message_id FROM communication_messages").fetchall()}
        assert "message-ended-archived-scene" not in remaining
        assert "message-ended" in remaining
        assert "message-archived-scene" in remaining
    with _connect(paths.chat_bubbles_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT bubble_id FROM chat_bubble_messages").fetchall()}
        assert "bubble-ended-archived-scene" not in remaining
        assert "bubble-ended" in remaining
        assert "bubble-archived-scene" in remaining
    with _connect(paths.llm_http_trace_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT llm_request_id FROM llm_http_traces").fetchall()}
        assert "trace-ended-archived-scene" not in remaining
        assert "trace-ended" in remaining
        assert "trace-archived-scene" in remaining
    with _connect(paths.terminal_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT terminal_id FROM terminals").fetchall()}
        assert "terminal-ended-archived-scene" not in remaining
        assert "terminal-ended" in remaining
        assert "terminal-archived-scene" in remaining


def test_logs_workspace_rest_contracts_preserve_trace_lookup_and_cleanup_flow(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_lifecycle(paths)
    _seed_logs(paths)
    with _connect(paths.llm_http_trace_db) as conn:
        conn.execute("DROP TABLE llm_http_traces")

    trace_repository = SqliteLlmHttpTraceRepository(lambda: str(paths.llm_http_trace_db.parent))
    now = datetime.now(timezone.utc)
    trace_repository.save_completed(
        LlmHttpTraceRecord(
            llm_request_id="trace-contract",
            llm_call_id="call-contract",
            conversation_id=None,
            task_id="task-completed",
            instance_id=None,
            trace_id=None,
            provider_id="provider",
            binding_id=None,
            model_id="model",
            model_alias=None,
            api_format="openai",
            candidate_index=None,
            api_key_index=None,
            retry_index=None,
            attempt_count=1,
            status="completed",
            method="POST",
            url="https://example.invalid/v1/chat/completions",
            request_headers={},
            request_body_text="{}",
            request_body_bytes=2,
            request_body_truncated=False,
            status_code=200,
            response_headers={},
            response_body_text="{}",
            response_body_bytes=2,
            response_body_truncated=False,
            response_content_type="application/json",
            is_stream=False,
            sse_event_count=None,
            error_type=None,
            error_message=None,
            started_at=now,
            completed_at=now,
            duration_ms=1,
            raw_request_text=None,
            raw_response_text=None,
        )
    )
    trace_repository.close()

    app = FastAPI()
    app.include_router(create_logs_management_router(_service(paths)), prefix="/api/logs")
    app.include_router(
        create_llm_http_trace_router(
            LlmHttpTraceService(
                SqliteLlmHttpTraceRepository(lambda: str(paths.llm_http_trace_db.parent)),
                LlmHttpTraceConfig(enabled=True, data_dir=str(paths.llm_http_trace_db.parent), max_body_bytes=1024),
            )
        ),
        prefix="/api/llm-http-traces",
    )
    client = TestClient(app)

    overview = client.get("/api/logs/overview")
    assert overview.status_code == 200
    assert len(overview.json()["items"]) == 5
    assert overview.json()["total_size_bytes"] > 0
    trace = client.get("/api/llm-http-traces/trace-contract")
    assert trace.status_code == 200
    assert trace.json()["llm_request_id"] == "trace-contract"

    request = {
        "lifecycle_filters": [LIFECYCLE_ENDED_TASKS],
        "categories": [CATEGORY_COMMUNICATION_MESSAGES, CATEGORY_CHAT_BUBBLES],
    }
    preview = client.post("/api/logs/cleanup/preview", json=request)
    assert preview.status_code == 200
    assert preview.json()["total_matched_count"] == 4
    execute = client.post("/api/logs/cleanup/execute", json=request)
    assert execute.status_code == 200
    assert execute.json()["total_deleted_count"] == preview.json()["total_matched_count"]
    with _connect(paths.llm_http_trace_db) as conn:
        remaining = {row[0] for row in conn.execute("SELECT llm_request_id FROM llm_http_traces").fetchall()}
        assert "trace-contract" in remaining
