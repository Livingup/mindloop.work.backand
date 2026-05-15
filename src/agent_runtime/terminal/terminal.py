from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final, TypeAlias

TerminalStatus: TypeAlias = str

TERMINAL_STATUS_IDLE: Final[str] = "idle"
TERMINAL_STATUS_BUSY: Final[str] = "busy"
TERMINAL_STATUS_DELETED: Final[str] = "deleted"
TERMINAL_STATUS_ERROR: Final[str] = "error"

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({
    TERMINAL_STATUS_IDLE,
    TERMINAL_STATUS_BUSY,
    TERMINAL_STATUS_DELETED,
    TERMINAL_STATUS_ERROR,
})

_TERMINAL_ACTIVE_STATUSES: Final[frozenset[str]] = frozenset({
    TERMINAL_STATUS_IDLE,
    TERMINAL_STATUS_BUSY,
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


@dataclass(frozen=True, slots=True)
class TerminalInstance:
    terminal_id: str
    owner_type: str
    owner_id: str
    cwd: str | None
    shell: str
    status: TerminalStatus = TERMINAL_STATUS_IDLE
    current_command_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_active_at: datetime | None = None
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.terminal_id, "terminal_id")
        require_non_blank(self.owner_type, "owner_type")
        require_non_blank(self.owner_id, "owner_id")
        require_non_blank(self.shell, "shell")
        if self.cwd is not None:
            require_non_blank(self.cwd, "cwd")
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"未知终端状态: {self.status}")
        if self.current_command_id is not None:
            require_non_blank(self.current_command_id, "current_command_id")
        now = utc_now()
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at or now)
        if self.last_active_at is None:
            object.__setattr__(self, "last_active_at", self.updated_at or now)
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})

    @property
    def is_active(self) -> bool:
        return self.status in _TERMINAL_ACTIVE_STATUSES

    @property
    def is_available(self) -> bool:
        return self.status == TERMINAL_STATUS_IDLE

    def mark_busy(self, command_id: str, *, cwd: str | None = None, at: datetime | None = None) -> "TerminalInstance":
        if self.status != TERMINAL_STATUS_IDLE:
            raise ValueError(f"终端当前不可执行命令: terminal_id={self.terminal_id} status={self.status}")
        now = at or utc_now()
        return replace(
            self,
            status=TERMINAL_STATUS_BUSY,
            current_command_id=command_id,
            cwd=cwd if cwd is not None else self.cwd,
            updated_at=now,
            last_active_at=now,
        )

    def mark_idle(self, *, at: datetime | None = None) -> "TerminalInstance":
        now = at or utc_now()
        return replace(
            self,
            status=TERMINAL_STATUS_IDLE,
            current_command_id=None,
            updated_at=now,
            last_active_at=now,
        )

    def delete(self, *, at: datetime | None = None) -> "TerminalInstance":
        now = at or utc_now()
        return replace(self, status=TERMINAL_STATUS_DELETED, current_command_id=None, updated_at=now, last_active_at=now)

    def mark_error(self, *, at: datetime | None = None) -> "TerminalInstance":
        now = at or utc_now()
        return replace(self, status=TERMINAL_STATUS_ERROR, current_command_id=None, updated_at=now, last_active_at=now)
