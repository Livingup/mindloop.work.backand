from __future__ import annotations

from typing import Protocol

from agent_runtime.terminal.terminal import TerminalInstance
from agent_runtime.terminal.terminal_command import TerminalCommand
from agent_runtime.terminal.terminal_output import TerminalOutputChunk


class TerminalRepository(Protocol):
    def reset_runtime_state(self) -> int:
        ...

    def save_terminal(self, terminal: TerminalInstance) -> TerminalInstance:
        ...

    def get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        ...

    def list_terminals_by_owner(
        self,
        owner_type: str,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[TerminalInstance]:
        ...

    def find_idle_terminal(self, owner_type: str, owner_id: str) -> TerminalInstance | None:
        ...

    def save_command(self, command: TerminalCommand) -> TerminalCommand:
        ...

    def get_command(self, command_id: str) -> TerminalCommand | None:
        ...

    def list_commands_by_terminal(self, terminal_id: str, *, limit: int = 100) -> list[TerminalCommand]:
        ...

    def append_output_chunk(self, chunk: TerminalOutputChunk) -> TerminalOutputChunk:
        ...

    def next_output_chunk_index(self, terminal_id: str, command_id: str) -> int:
        ...

    def list_output_chunks(
        self,
        terminal_id: str,
        *,
        command_id: str | None = None,
        after_chunk_index: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[TerminalOutputChunk]:
        ...

    def search_output_chunks(
        self,
        terminal_id: str,
        query: str,
        *,
        command_id: str | None = None,
        case_sensitive: bool = False,
        limit: int = 100,
    ) -> list[TerminalOutputChunk]:
        ...
