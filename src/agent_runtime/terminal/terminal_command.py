from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, TypeAlias

from agent_runtime.terminal.terminal import require_non_blank, utc_now

TerminalCommandStatus: TypeAlias = str

TERMINAL_COMMAND_STATUS_ACCEPTED: Final[str] = "accepted"
TERMINAL_COMMAND_STATUS_RUNNING: Final[str] = "running"
TERMINAL_COMMAND_STATUS_COMPLETED: Final[str] = "completed"
TERMINAL_COMMAND_STATUS_FAILED: Final[str] = "failed"
TERMINAL_COMMAND_STATUS_CANCELLED: Final[str] = "cancelled"
TERMINAL_COMMAND_STATUS_TIMEOUT: Final[str] = "timeout"

TERMINAL_COMMAND_STATUSES: Final[frozenset[str]] = frozenset({
    TERMINAL_COMMAND_STATUS_ACCEPTED,
    TERMINAL_COMMAND_STATUS_RUNNING,
    TERMINAL_COMMAND_STATUS_COMPLETED,
    TERMINAL_COMMAND_STATUS_FAILED,
    TERMINAL_COMMAND_STATUS_CANCELLED,
    TERMINAL_COMMAND_STATUS_TIMEOUT,
})


@dataclass(frozen=True, slots=True)
class TerminalCommand:
    command_id: str
    terminal_id: str
    command_line: str
    actual_cwd: str
    shell: str
    status: TerminalCommandStatus = TERMINAL_COMMAND_STATUS_ACCEPTED
    requested_cwd: str | None = None
    tool_call_id: str | None = None
    agent_instance_id: str | None = None
    trace_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.command_id, "command_id")
        require_non_blank(self.terminal_id, "terminal_id")
        require_non_blank(self.command_line, "command_line")
        require_non_blank(self.actual_cwd, "actual_cwd")
        require_non_blank(self.shell, "shell")
        if self.status not in TERMINAL_COMMAND_STATUSES:
            raise ValueError(f"未知终端命令状态: {self.status}")
        if self.requested_cwd is not None:
            require_non_blank(self.requested_cwd, "requested_cwd")
        if self.tool_call_id is not None:
            require_non_blank(self.tool_call_id, "tool_call_id")
        if self.agent_instance_id is not None:
            require_non_blank(self.agent_instance_id, "agent_instance_id")
        if self.trace_id is not None:
            require_non_blank(self.trace_id, "trace_id")
        if self.stdout_bytes < 0 or self.stderr_bytes < 0:
            raise ValueError("输出字节数不能为负数。")
        now = utc_now()
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at or now)

    def mark_running(self, *, at: datetime | None = None) -> "TerminalCommand":
        now = at or utc_now()
        return replace(self, status=TERMINAL_COMMAND_STATUS_RUNNING, started_at=self.started_at or now, updated_at=now)

    def complete(
        self,
        *,
        exit_code: int | None,
        stdout_bytes: int,
        stderr_bytes: int,
        at: datetime | None = None,
    ) -> "TerminalCommand":
        now = at or utc_now()
        status = TERMINAL_COMMAND_STATUS_COMPLETED if exit_code == 0 else TERMINAL_COMMAND_STATUS_FAILED
        duration_ms = None
        if self.started_at is not None:
            duration_ms = int((now - self.started_at).total_seconds() * 1000)
        return replace(
            self,
            status=status,
            completed_at=now,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            updated_at=now,
        )

    def fail(
        self,
        error_message: str,
        *,
        exit_code: int | None = None,
        stdout_bytes: int | None = None,
        stderr_bytes: int | None = None,
        at: datetime | None = None,
    ) -> "TerminalCommand":
        now = at or utc_now()
        duration_ms = None
        if self.started_at is not None:
            duration_ms = int((now - self.started_at).total_seconds() * 1000)
        return replace(
            self,
            status=TERMINAL_COMMAND_STATUS_FAILED,
            completed_at=now,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=self.stdout_bytes if stdout_bytes is None else stdout_bytes,
            stderr_bytes=self.stderr_bytes if stderr_bytes is None else stderr_bytes,
            error_message=error_message,
            updated_at=now,
        )

    def cancel(
        self,
        error_message: str,
        *,
        exit_code: int | None = None,
        stdout_bytes: int | None = None,
        stderr_bytes: int | None = None,
        at: datetime | None = None,
    ) -> "TerminalCommand":
        now = at or utc_now()
        duration_ms = None
        if self.started_at is not None:
            duration_ms = int((now - self.started_at).total_seconds() * 1000)
        return replace(
            self,
            status=TERMINAL_COMMAND_STATUS_CANCELLED,
            completed_at=now,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=self.stdout_bytes if stdout_bytes is None else stdout_bytes,
            stderr_bytes=self.stderr_bytes if stderr_bytes is None else stderr_bytes,
            error_message=error_message,
            updated_at=now,
        )

    def timeout(
        self,
        error_message: str,
        *,
        exit_code: int | None = None,
        stdout_bytes: int | None = None,
        stderr_bytes: int | None = None,
        at: datetime | None = None,
    ) -> "TerminalCommand":
        now = at or utc_now()
        duration_ms = None
        if self.started_at is not None:
            duration_ms = int((now - self.started_at).total_seconds() * 1000)
        return replace(
            self,
            status=TERMINAL_COMMAND_STATUS_TIMEOUT,
            completed_at=now,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=self.stdout_bytes if stdout_bytes is None else stdout_bytes,
            stderr_bytes=self.stderr_bytes if stderr_bytes is None else stderr_bytes,
            error_message=error_message,
            updated_at=now,
        )
