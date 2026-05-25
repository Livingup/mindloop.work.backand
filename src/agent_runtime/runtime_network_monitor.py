from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

NETWORK_LOG_MAX_BYTES = 10 * 1024 * 1024
NETWORK_LOG_MAX_FILES = 5
NETWORK_LOG_QUEUE_SIZE = 2000
NETWORK_LOG_BATCH_SIZE = 100
NETWORK_LOG_FLUSH_INTERVAL_SECONDS = 1.0
NETWORK_SNAPSHOT_INTERVAL_SECONDS = 10.0
NETWORK_MAX_STRING_LENGTH = 500


class RuntimeNetworkMonitor:
    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=NETWORK_LOG_QUEUE_SIZE)
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=500)
        self._lock = threading.RLock()
        self._writer_thread: threading.Thread | None = None
        self._snapshot_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_http_requests = 0
        self._active_sse_connections = 0
        self._dropped_log_records = 0
        self._event_hub_subscribers = 0
        self._event_hub_queue_full_count = 0

    def start(self) -> None:
        with self._lock:
            if self._writer_thread and self._writer_thread.is_alive():
                return
            self._stop_event.clear()
            self._writer_thread = threading.Thread(target=self._writer_loop, name="runtime-network-log-writer", daemon=True)
            self._snapshot_thread = threading.Thread(target=self._snapshot_loop, name="runtime-network-snapshot", daemon=True)
            self._writer_thread.start()
            self._snapshot_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            writer_thread = self._writer_thread
            snapshot_thread = self._snapshot_thread
        if snapshot_thread is not None:
            snapshot_thread.join(timeout=2)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if writer_thread is not None:
            writer_thread.join(timeout=5)

    def begin_http_request(self) -> int:
        with self._lock:
            self._active_http_requests += 1
            return self._active_http_requests

    def end_http_request(self) -> int:
        with self._lock:
            self._active_http_requests = max(self._active_http_requests - 1, 0)
            return self._active_http_requests

    def begin_sse_connection(self) -> int:
        with self._lock:
            self._active_sse_connections += 1
            return self._active_sse_connections

    def end_sse_connection(self) -> int:
        with self._lock:
            self._active_sse_connections = max(self._active_sse_connections - 1, 0)
            return self._active_sse_connections

    def event_hub_subscribed(self) -> int:
        with self._lock:
            self._event_hub_subscribers += 1
            return self._event_hub_subscribers

    def event_hub_unsubscribed(self) -> int:
        with self._lock:
            self._event_hub_subscribers = max(self._event_hub_subscribers - 1, 0)
            return self._event_hub_subscribers

    def event_hub_queue_full(self, count: int = 1) -> int:
        with self._lock:
            self._event_hub_queue_full_count += max(count, 0)
            return self._event_hub_queue_full_count

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active_http_requests": self._active_http_requests,
                "active_sse_connections": self._active_sse_connections,
                "event_hub_subscribers": self._event_hub_subscribers,
                "event_hub_queue_full_count": self._event_hub_queue_full_count,
                "dropped_log_records": self._dropped_log_records,
                "queued_log_records": self._queue.qsize(),
            }

    def record(self, event: dict[str, Any]) -> None:
        record = _sanitize_record({
            **event,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int((time.time() % 1) * 1000):03d}",
        })
        with self._lock:
            self._recent_events.append(record)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._dropped_log_records += 1

    def _snapshot_loop(self) -> None:
        while not self._stop_event.wait(NETWORK_SNAPSHOT_INTERVAL_SECONDS):
            self.record({
                "layer": "backend",
                "type": "network.snapshot",
                **self.snapshot(),
            })

    def _writer_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + NETWORK_LOG_FLUSH_INTERVAL_SECONDS
        while True:
            timeout = max(deadline - time.monotonic(), 0.0)
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is None:
                if batch:
                    self._write_batch(batch)
                    batch.clear()
                if self._stop_event.is_set():
                    break
                deadline = time.monotonic() + NETWORK_LOG_FLUSH_INTERVAL_SECONDS
                continue
            batch.append(item)
            if len(batch) >= NETWORK_LOG_BATCH_SIZE:
                self._write_batch(batch)
                batch.clear()
                deadline = time.monotonic() + NETWORK_LOG_FLUSH_INTERVAL_SECONDS

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            file_path = _network_log_file_path()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_log_if_needed(file_path)
            with file_path.open("a", encoding="utf-8") as file:
                for item in batch:
                    file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                    file.write("\n")
        except Exception:
            with self._lock:
                self._dropped_log_records += len(batch)


runtime_network_monitor = RuntimeNetworkMonitor()


def new_network_trace_id(prefix: str = "backend") -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


def _network_log_file_path() -> Path:
    stderr_log = os.environ.get("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG")
    if stderr_log:
        return Path(stderr_log).resolve().parent / "network-backend.jsonl"
    return Path(tempfile.gettempdir()) / "spread-ai" / "network-backend.jsonl"


def _rotate_log_if_needed(file_path: Path) -> None:
    try:
        if not file_path.exists() or file_path.stat().st_size < NETWORK_LOG_MAX_BYTES:
            return
        for index in range(NETWORK_LOG_MAX_FILES - 1, 0, -1):
            source = file_path.with_name(f"{file_path.name}.{index}")
            target = file_path.with_name(f"{file_path.name}.{index + 1}")
            if index == NETWORK_LOG_MAX_FILES - 1 and target.exists():
                target.unlink(missing_ok=True)
            if source.exists():
                source.replace(target)
        file_path.replace(file_path.with_name(f"{file_path.name}.1"))
    except Exception:
        return


def _sanitize_record(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [_sanitize_record(item) for item in value[:20]]
    if isinstance(value, str):
        return _sanitize_string(value)
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, raw_value in value.items():
        lower_key = str(key).lower()
        if "body" in lower_key or "payload" in lower_key:
            continue
        if "authorization" in lower_key or "cookie" in lower_key:
            continue
        result[str(key)] = _sanitize_record(raw_value)
    return result


def _sanitize_string(value: str) -> str:
    trimmed = value if len(value) <= NETWORK_MAX_STRING_LENGTH else f"{value[:NETWORK_MAX_STRING_LENGTH]}…"
    for marker in ("api_key=", "apikey=", "token=", "access_token=", "refresh_token=", "key="):
        index = trimmed.lower().find(marker)
        if index >= 0:
            end = trimmed.find("&", index)
            if end < 0:
                return f"{trimmed[:index + len(marker)]}<redacted>"
            return f"{trimmed[:index + len(marker)]}<redacted>{trimmed[end:]}"
    return trimmed
