from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

TerminalOutputHandler = Callable[[str, str], Awaitable[None]]
TerminalExitHandler = Callable[[int | None, str | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TerminalProcessStartRequest:
    terminal_id: str
    command_id: str
    command_line: str
    cwd: str
    shell: str
    timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TerminalSessionStartRequest:
    terminal_id: str
    cwd: str
    shell: str
    runtime_token: str | None = None
    cols: int = 120
    rows: int = 30


class TerminalProcessManager(Protocol):
    async def start_terminal(
        self,
        request: TerminalSessionStartRequest,
        *,
        on_exit: TerminalExitHandler,
    ) -> bool:
        ...

    async def start_command(
        self,
        request: TerminalProcessStartRequest,
        *,
        on_output: TerminalOutputHandler,
        on_exit: TerminalExitHandler,
    ) -> None:
        ...

    async def write_input(self, terminal_id: str, text: str) -> int:
        ...

    async def interrupt_terminal(self, terminal_id: str) -> None:
        ...

    async def terminate_terminal(self, terminal_id: str) -> None:
        ...

    async def shutdown(self) -> None:
        ...
