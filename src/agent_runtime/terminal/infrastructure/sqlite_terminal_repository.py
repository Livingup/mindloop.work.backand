from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.infrastructure.sqlite_maintenance import coordinated_sqlite_repository
from agent_runtime.terminal.terminal import (
    TERMINAL_STATUS_BUSY,
    TERMINAL_STATUS_DELETED,
    TERMINAL_STATUS_ERROR,
    TERMINAL_STATUS_IDLE,
    TerminalInstance,
    utc_now,
)
from agent_runtime.terminal.terminal_command import (
    TERMINAL_COMMAND_STATUS_ACCEPTED,
    TERMINAL_COMMAND_STATUS_FAILED,
    TERMINAL_COMMAND_STATUS_RUNNING,
    TerminalCommand,
)
from agent_runtime.terminal.terminal_output import TerminalOutputChunk

_TERMINAL_OUTPUT_LINE_RETENTION_LIMIT = 5000
_SQLITE_DELETE_BATCH_SIZE = 500
_LINE_BREAK_PATTERN = re.compile(r"\r\n|\r|\n")
_LEGACY_TERMINAL_STATUS_CLOSED = "closed"
_LEGACY_TERMINAL_STATUS_WAITING_INPUT = "waiting_input"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS terminals (
    terminal_id        TEXT PRIMARY KEY,
    owner_type         TEXT NOT NULL,
    owner_id           TEXT NOT NULL,
    cwd                TEXT,
    shell              TEXT NOT NULL,
    status             TEXT NOT NULL,
    current_command_id TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_active_at     TEXT NOT NULL,
    metadata_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_terminals_owner ON terminals(owner_type, owner_id, status, last_active_at);

CREATE TABLE IF NOT EXISTS terminal_commands (
    command_id        TEXT PRIMARY KEY,
    terminal_id       TEXT NOT NULL,
    command_line      TEXT NOT NULL,
    requested_cwd     TEXT,
    actual_cwd        TEXT NOT NULL,
    shell             TEXT NOT NULL,
    status            TEXT NOT NULL,
    tool_call_id      TEXT,
    agent_instance_id TEXT,
    trace_id          TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    exit_code         INTEGER,
    duration_ms       INTEGER,
    stdout_bytes      INTEGER NOT NULL,
    stderr_bytes      INTEGER NOT NULL,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY(terminal_id) REFERENCES terminals(terminal_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_terminal_commands_terminal ON terminal_commands(terminal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_terminal_commands_status ON terminal_commands(status);

CREATE TABLE IF NOT EXISTS terminal_output_chunks (
    terminal_id  TEXT NOT NULL,
    command_id   TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    byte_offset  INTEGER NOT NULL,
    stream       TEXT NOT NULL,
    chunk        TEXT NOT NULL,
    encoding     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY(terminal_id, command_id, chunk_index),
    FOREIGN KEY(terminal_id) REFERENCES terminals(terminal_id) ON DELETE CASCADE,
    FOREIGN KEY(command_id) REFERENCES terminal_commands(command_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_terminal_output_terminal ON terminal_output_chunks(terminal_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_terminal_output_command ON terminal_output_chunks(command_id, chunk_index);
"""


@coordinated_sqlite_repository(
    lambda repository: Path(repository._data_dir_accessor()).expanduser() / "terminal.db",
 )
class SqliteTerminalRepository:
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

    def reset_runtime_state(self) -> int:
        with self._lock:
            now = _datetime_to_text(utc_now())
            conn = self._connection()
            terminal_result = conn.execute(
                """
                UPDATE terminals
                SET status = ?, current_command_id = NULL, updated_at = ?
                WHERE status IN (?, ?, ?, ?) OR (status = ? AND current_command_id IS NOT NULL)
                """,
                (
                    TERMINAL_STATUS_IDLE,
                    now,
                    TERMINAL_STATUS_BUSY,
                    _LEGACY_TERMINAL_STATUS_WAITING_INPUT,
                    TERMINAL_STATUS_ERROR,
                    _LEGACY_TERMINAL_STATUS_CLOSED,
                    TERMINAL_STATUS_IDLE,
                ),
            )
            command_result = conn.execute(
                """
                UPDATE terminal_commands
                SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ?, error_message = COALESCE(error_message, ?)
                WHERE status IN (?, ?)
                """,
                (
                    TERMINAL_COMMAND_STATUS_FAILED,
                    now,
                    now,
                    "AgentRuntime restarted; terminal runtime state was reset.",
                    TERMINAL_COMMAND_STATUS_ACCEPTED,
                    TERMINAL_COMMAND_STATUS_RUNNING,
                ),
            )
            conn.commit()
            return int(terminal_result.rowcount or 0) + int(command_result.rowcount or 0)

    def save_terminal(self, terminal: TerminalInstance) -> TerminalInstance:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO terminals (
                    terminal_id, owner_type, owner_id, cwd, shell, status,
                    current_command_id, created_at, updated_at, last_active_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(terminal_id) DO UPDATE SET
                    owner_type = excluded.owner_type,
                    owner_id = excluded.owner_id,
                    cwd = excluded.cwd,
                    shell = excluded.shell,
                    status = excluded.status,
                    current_command_id = excluded.current_command_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    last_active_at = excluded.last_active_at,
                    metadata_json = excluded.metadata_json
                """,
                _terminal_to_row(terminal),
            )
            conn.commit()
            return terminal

    def delete_terminal_data(self, terminal_id: str) -> int:
        with self._lock:
            conn = self._connection()
            chunk_result = conn.execute(
                "DELETE FROM terminal_output_chunks WHERE terminal_id = ?",
                (terminal_id,),
            )
            command_result = conn.execute(
                "DELETE FROM terminal_commands WHERE terminal_id = ?",
                (terminal_id,),
            )
            conn.commit()
            return int(chunk_result.rowcount or 0) + int(command_result.rowcount or 0)

    def get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                       current_command_id, created_at, updated_at, last_active_at, metadata_json
                FROM terminals WHERE terminal_id = ?
                """,
                (terminal_id,),
            ).fetchone()
            return _row_to_terminal(row) if row else None

    def list_terminals_by_owner(
        self,
        owner_type: str,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[TerminalInstance]:
        with self._lock:
            if include_deleted:
                rows = self._connection().execute(
                    """
                    SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                           current_command_id, created_at, updated_at, last_active_at, metadata_json
                    FROM terminals
                    WHERE owner_type = ? AND owner_id = ?
                    ORDER BY last_active_at DESC
                    """,
                    (owner_type, owner_id),
                ).fetchall()
            else:
                rows = self._connection().execute(
                    """
                    SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                           current_command_id, created_at, updated_at, last_active_at, metadata_json
                    FROM terminals
                    WHERE owner_type = ? AND owner_id = ? AND status != ?
                    ORDER BY last_active_at DESC
                    """,
                    (owner_type, owner_id, TERMINAL_STATUS_DELETED),
                ).fetchall()
            return [_row_to_terminal(row) for row in rows]

    def list_terminals_by_task_id(
        self,
        task_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[TerminalInstance]:
        with self._lock:
            if include_deleted:
                rows = self._connection().execute(
                    """
                    SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                           current_command_id, created_at, updated_at, last_active_at, metadata_json
                    FROM terminals
                    WHERE json_extract(metadata_json, '$.task_id') = ?
                    ORDER BY last_active_at DESC
                    """,
                    (task_id,),
                ).fetchall()
            else:
                rows = self._connection().execute(
                    """
                    SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                           current_command_id, created_at, updated_at, last_active_at, metadata_json
                    FROM terminals
                    WHERE json_extract(metadata_json, '$.task_id') = ? AND status != ?
                    ORDER BY last_active_at DESC
                    """,
                    (task_id, TERMINAL_STATUS_DELETED),
                ).fetchall()
            return [_row_to_terminal(row) for row in rows]

    def find_idle_terminal(self, owner_type: str, owner_id: str) -> TerminalInstance | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT terminal_id, owner_type, owner_id, cwd, shell, status,
                       current_command_id, created_at, updated_at, last_active_at, metadata_json
                FROM terminals
                WHERE owner_type = ? AND owner_id = ? AND status = 'idle'
                ORDER BY last_active_at DESC
                LIMIT 1
                """,
                (owner_type, owner_id),
            ).fetchone()
            return _row_to_terminal(row) if row else None

    def save_command(self, command: TerminalCommand) -> TerminalCommand:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO terminal_commands (
                    command_id, terminal_id, command_line, requested_cwd, actual_cwd, shell, status,
                    tool_call_id, agent_instance_id, trace_id, started_at, completed_at, exit_code,
                    duration_ms, stdout_bytes, stderr_bytes, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                    terminal_id = excluded.terminal_id,
                    command_line = excluded.command_line,
                    requested_cwd = excluded.requested_cwd,
                    actual_cwd = excluded.actual_cwd,
                    shell = excluded.shell,
                    status = excluded.status,
                    tool_call_id = excluded.tool_call_id,
                    agent_instance_id = excluded.agent_instance_id,
                    trace_id = excluded.trace_id,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    exit_code = excluded.exit_code,
                    duration_ms = excluded.duration_ms,
                    stdout_bytes = excluded.stdout_bytes,
                    stderr_bytes = excluded.stderr_bytes,
                    error_message = excluded.error_message,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                _command_to_row(command),
            )
            conn.commit()
            return command

    def get_command(self, command_id: str) -> TerminalCommand | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT command_id, terminal_id, command_line, requested_cwd, actual_cwd, shell, status,
                       tool_call_id, agent_instance_id, trace_id, started_at, completed_at, exit_code,
                       duration_ms, stdout_bytes, stderr_bytes, error_message, created_at, updated_at
                FROM terminal_commands WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            return _row_to_command(row) if row else None

    def list_commands_by_terminal(self, terminal_id: str, *, limit: int = 100) -> list[TerminalCommand]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT command_id, terminal_id, command_line, requested_cwd, actual_cwd, shell, status,
                       tool_call_id, agent_instance_id, trace_id, started_at, completed_at, exit_code,
                       duration_ms, stdout_bytes, stderr_bytes, error_message, created_at, updated_at
                FROM terminal_commands
                WHERE terminal_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (terminal_id, max(1, min(limit, 500))),
            ).fetchall()
            return [_row_to_command(row) for row in rows]

    def append_output_chunk(self, chunk: TerminalOutputChunk) -> TerminalOutputChunk:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO terminal_output_chunks (
                    terminal_id, command_id, chunk_index, byte_offset, stream, chunk, encoding, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _chunk_to_row(chunk),
            )
            _prune_terminal_output_lines(conn, chunk.terminal_id, _TERMINAL_OUTPUT_LINE_RETENTION_LIMIT)
            conn.commit()
            return chunk

    def next_output_chunk_index(self, terminal_id: str, command_id: str) -> int:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT COALESCE(MAX(chunk_index), -1) + 1
                FROM terminal_output_chunks
                WHERE terminal_id = ? AND command_id = ?
                """,
                (terminal_id, command_id),
            ).fetchone()
            return int(row[0])

    def list_output_chunks(
        self,
        terminal_id: str,
        *,
        command_id: str | None = None,
        after_chunk_index: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[TerminalOutputChunk]:
        with self._lock:
            clauses = ["terminal_id = ?"]
            params: list[Any] = [terminal_id]
            if command_id is not None:
                clauses.append("command_id = ?")
                params.append(command_id)
            if after_chunk_index is not None:
                clauses.append("chunk_index > ?")
                params.append(after_chunk_index)
            params.extend([max(1, min(limit, 5000)), max(offset, 0)])
            order_by = "chunk_index ASC" if command_id is not None else "created_at ASC, rowid ASC"
            rows = self._connection().execute(
                f"""
                SELECT terminal_id, command_id, chunk_index, byte_offset, stream, chunk, encoding, created_at
                FROM terminal_output_chunks
                WHERE {' AND '.join(clauses)}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
            return [_row_to_chunk(row) for row in rows]

    def search_output_chunks(
        self,
        terminal_id: str,
        query: str,
        *,
        command_id: str | None = None,
        case_sensitive: bool = False,
        limit: int = 100,
    ) -> list[TerminalOutputChunk]:
        with self._lock:
            clauses = ["terminal_id = ?"]
            params: list[Any] = [terminal_id]
            if command_id is not None:
                clauses.append("command_id = ?")
                params.append(command_id)
            if case_sensitive:
                clauses.append("instr(chunk, ?) > 0")
                params.append(query)
            else:
                clauses.append("lower(chunk) LIKE ?")
                params.append(f"%{query.lower()}%")
            params.append(max(1, min(limit, 1000)))
            order_by = "chunk_index ASC" if command_id is not None else "created_at ASC, rowid ASC"
            rows = self._connection().execute(
                f"""
                SELECT terminal_id, command_id, chunk_index, byte_offset, stream, chunk, encoding, created_at
                FROM terminal_output_chunks
                WHERE {' AND '.join(clauses)}
                ORDER BY {order_by}
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [_row_to_chunk(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "terminal.db"
        if self._conn is not None and self._db_path == db_path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._db_path = db_path
        return self._conn


def _terminal_to_row(terminal: TerminalInstance) -> tuple[Any, ...]:
    return (
        terminal.terminal_id,
        terminal.owner_type,
        terminal.owner_id,
        terminal.cwd,
        terminal.shell,
        terminal.status,
        terminal.current_command_id,
        _datetime_to_text(terminal.created_at),
        _datetime_to_text(terminal.updated_at),
        _datetime_to_text(terminal.last_active_at),
        json.dumps(terminal.metadata or {}, ensure_ascii=False),
    )


def _row_to_terminal(row: tuple[Any, ...]) -> TerminalInstance:
    status = row[5]
    current_command_id = row[6]
    if status in {_LEGACY_TERMINAL_STATUS_CLOSED, _LEGACY_TERMINAL_STATUS_WAITING_INPUT}:
        status = TERMINAL_STATUS_IDLE
        current_command_id = None
    return TerminalInstance(
        terminal_id=row[0],
        owner_type=row[1],
        owner_id=row[2],
        cwd=row[3],
        shell=row[4],
        status=status,
        current_command_id=current_command_id,
        created_at=_text_to_datetime(row[7]),
        updated_at=_text_to_datetime(row[8]),
        last_active_at=_text_to_datetime(row[9]),
        metadata=json.loads(row[10]),
    )


def _command_to_row(command: TerminalCommand) -> tuple[Any, ...]:
    return (
        command.command_id,
        command.terminal_id,
        command.command_line,
        command.requested_cwd,
        command.actual_cwd,
        command.shell,
        command.status,
        command.tool_call_id,
        command.agent_instance_id,
        command.trace_id,
        _datetime_to_text(command.started_at),
        _datetime_to_text(command.completed_at),
        command.exit_code,
        command.duration_ms,
        command.stdout_bytes,
        command.stderr_bytes,
        command.error_message,
        _datetime_to_text(command.created_at),
        _datetime_to_text(command.updated_at),
    )


def _row_to_command(row: tuple[Any, ...]) -> TerminalCommand:
    return TerminalCommand(
        command_id=row[0],
        terminal_id=row[1],
        command_line=row[2],
        requested_cwd=row[3],
        actual_cwd=row[4],
        shell=row[5],
        status=row[6],
        tool_call_id=row[7],
        agent_instance_id=row[8],
        trace_id=row[9],
        started_at=_text_to_datetime(row[10]),
        completed_at=_text_to_datetime(row[11]),
        exit_code=row[12],
        duration_ms=row[13],
        stdout_bytes=int(row[14]),
        stderr_bytes=int(row[15]),
        error_message=row[16],
        created_at=_text_to_datetime(row[17]),
        updated_at=_text_to_datetime(row[18]),
    )


def _chunk_to_row(chunk: TerminalOutputChunk) -> tuple[Any, ...]:
    return (
        chunk.terminal_id,
        chunk.command_id,
        chunk.chunk_index,
        chunk.byte_offset,
        chunk.stream,
        chunk.chunk,
        chunk.encoding,
        _datetime_to_text(chunk.created_at),
    )


def _row_to_chunk(row: tuple[Any, ...]) -> TerminalOutputChunk:
    return TerminalOutputChunk(
        terminal_id=row[0],
        command_id=row[1],
        chunk_index=int(row[2]),
        byte_offset=int(row[3]),
        stream=row[4],
        chunk=row[5],
        encoding=row[6],
        created_at=_text_to_datetime(row[7]),
    )


def _prune_terminal_output_lines(conn: sqlite3.Connection, terminal_id: str, max_lines: int) -> None:
    rows = conn.execute(
        """
        SELECT rowid, chunk
        FROM terminal_output_chunks
        WHERE terminal_id = ?
        ORDER BY created_at DESC, rowid DESC
        """,
        (terminal_id,),
    ).fetchall()
    remaining_lines = max_lines
    rowids_to_delete: list[int] = []
    replacement_rowid: int | None = None
    replacement_chunk: str | None = None

    for rowid, chunk in rows:
        actual_rowid = int(rowid)
        line_count = _count_output_lines(chunk)
        if remaining_lines <= 0:
            rowids_to_delete.append(actual_rowid)
            continue
        if line_count <= remaining_lines:
            remaining_lines -= line_count
            continue
        replacement_rowid = actual_rowid
        replacement_chunk = _trim_text_to_latest_lines(chunk, remaining_lines)
        remaining_lines = 0

    if rowids_to_delete:
        for rowid_batch in _batched(rowids_to_delete, _SQLITE_DELETE_BATCH_SIZE):
            conn.execute(
                f"DELETE FROM terminal_output_chunks WHERE rowid IN ({','.join('?' for _ in rowid_batch)})",
                tuple(rowid_batch),
            )
    if replacement_rowid is not None and replacement_chunk is not None:
        if replacement_chunk:
            conn.execute(
                "UPDATE terminal_output_chunks SET chunk = ? WHERE rowid = ?",
                (replacement_chunk, replacement_rowid),
            )
        else:
            conn.execute("DELETE FROM terminal_output_chunks WHERE rowid = ?", (replacement_rowid,))


def _count_output_lines(text: str) -> int:
    if text == "":
        return 0
    lines = len(_LINE_BREAK_PATTERN.findall(text))
    if not _LINE_BREAK_PATTERN.search(text[-1]):
        lines += 1
    return lines


def _trim_text_to_latest_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    line_count = _count_output_lines(text)
    if line_count <= max_lines:
        return text
    lines_to_drop = line_count - max_lines
    for match in _LINE_BREAK_PATTERN.finditer(text):
        lines_to_drop -= 1
        if lines_to_drop <= 0:
            return text[match.end():]
    return ""


def _batched(values: list[int], batch_size: int) -> list[list[int]]:
    return [values[index:index + batch_size] for index in range(0, len(values), batch_size)]


def _datetime_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _text_to_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
