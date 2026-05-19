from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

ReadMode = Literal["tail", "page", "since", "search"]


@dataclass(frozen=True, slots=True)
class TerminalCommandInfo:
    command_id: str
    terminal_id: str
    command_line: str
    actual_cwd: str
    shell: str
    status: str
    requested_cwd: str | None = None
    tool_call_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    terminal_id: str
    cwd: str | None
    shell: str
    status: str
    current_command_id: str | None
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    latest_command: TerminalCommandInfo | None = None


@dataclass(frozen=True, slots=True)
class CreateTerminalRequest:
    cwd: str | None = None
    shell: str | None = None
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


@dataclass(frozen=True, slots=True)
class ExecuteTerminalCommandRequest:
    command_line: str
    terminal_id: str | None = None
    cwd: str | None = None
    shell: str | None = None
    timeout_ms: int | None = None
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


@dataclass(frozen=True, slots=True)
class ExecuteTerminalCommandResult:
    accepted: bool
    terminal_id: str
    command_id: str
    status: str
    actual_cwd: str
    shell: str


@dataclass(frozen=True, slots=True)
class ReadTerminalOutputRequest:
    terminal_id: str
    command_id: str | None = None
    mode: ReadMode = "tail"
    tail_lines: int = 80
    offset: int = 0
    limit_lines: int = 200
    limit_chars: int = 12000
    after_chunk_index: int | None = None
    wait_timeout_ms: int = 0
    query: str | None = None
    case_sensitive: bool = False
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


@dataclass(frozen=True, slots=True)
class ReadTerminalOutputResult:
    terminal_id: str
    command_id: str | None
    status: str
    text: str
    chunks_returned: int
    truncated: bool
    start_chunk_index: int | None
    end_chunk_index: int | None
    next_chunk_index: int | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class WriteTerminalInputRequest:
    terminal_id: str
    text: str | None = None
    append_newline: bool = False
    control: str | None = None
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


@dataclass(frozen=True, slots=True)
class WriteTerminalInputResult:
    accepted: bool
    terminal_id: str
    bytes_written: int


@dataclass(frozen=True, slots=True)
class ListTerminalsRequest:
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


@dataclass(frozen=True, slots=True)
class CloseTerminalRequest:
    terminal_id: str
    scene_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    workspace_path: str | None = None


class TerminalGateway(Protocol):
    async def create_terminal(self, request: CreateTerminalRequest) -> TerminalInfo:
        ...

    async def execute_command(self, request: ExecuteTerminalCommandRequest) -> ExecuteTerminalCommandResult:
        ...

    async def read_output(self, request: ReadTerminalOutputRequest) -> ReadTerminalOutputResult:
        ...

    async def write_input(self, request: WriteTerminalInputRequest) -> WriteTerminalInputResult:
        ...

    def list_terminals(self, request: ListTerminalsRequest) -> list[TerminalInfo]:
        ...

    async def close_terminal(self, request: CloseTerminalRequest) -> TerminalInfo:
        ...

    async def delete_terminal(self, request: CloseTerminalRequest) -> TerminalInfo:
        ...

    async def delete_task_terminals(self, task_id: str) -> list[TerminalInfo]:
        ...

    async def shutdown(self) -> None:
        ...
