import asyncio
import logging
import threading

import pytest

from agent_runtime.infrastructure.asyncio_diagnostics import (
    BlockingPool,
    configure_blocking_pool,
    shutdown_blocking_executors,
)
from agent_runtime.terminal.terminal import TerminalInstance
from agent_runtime.terminal.terminal_command import TerminalCommand
from agent_runtime.terminal.terminal_output import TerminalOutputChunk
from agent_runtime.terminal.terminal_process import TerminalProcessStartRequest, TerminalSessionStartRequest
from agent_runtime.terminal.terminal_service import (
    TerminalRuntimeScope,
    TerminalRuntimeScopeResolver,
    TerminalService,
)


class _FakeTerminalRepository:
    def __init__(self) -> None:
        self.terminal = TerminalInstance(
            terminal_id="term-1",
            owner_type="task",
            owner_id="task-1",
            cwd="D:/workspace",
            shell="powershell",
        )
        self.command = TerminalCommand(
            command_id="cmd-1",
            terminal_id="term-1",
            command_line="python app.py",
            actual_cwd="D:/workspace",
            shell="powershell",
        )
        self.chunks = [
            TerminalOutputChunk(
                terminal_id="term-1",
                command_id="cmd-1",
                chunk_index=0,
                byte_offset=0,
                stream="stdout",
                chunk="first line\n",
            ),
            TerminalOutputChunk(
                terminal_id="term-1",
                command_id="cmd-1",
                chunk_index=1,
                byte_offset=11,
                stream="stdout",
                chunk="match line\n",
            ),
            TerminalOutputChunk(
                terminal_id="term-1",
                command_id="cmd-1",
                chunk_index=2,
                byte_offset=22,
                stream="stdout",
                chunk="last line\n",
            ),
        ]
        self.list_threads: list[str] = []
        self.search_threads: list[str] = []

    def reset_runtime_state(self) -> int:
        return 0

    def save_terminal(self, terminal: TerminalInstance) -> TerminalInstance:
        self.terminal = terminal
        return terminal

    def delete_terminal_data(self, terminal_id: str) -> int:
        return 0

    def get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        return self.terminal if terminal_id == self.terminal.terminal_id else None

    def list_terminals_by_owner(self, owner_type: str, owner_id: str, *, include_deleted: bool = False):
        return [self.terminal] if owner_type == self.terminal.owner_type and owner_id == self.terminal.owner_id else []

    def list_terminals_by_task_id(self, task_id: str, *, include_deleted: bool = False):
        return [self.terminal] if task_id == self.terminal.owner_id else []

    def find_idle_terminal(self, owner_type: str, owner_id: str) -> TerminalInstance | None:
        return self.terminal

    def save_command(self, command: TerminalCommand) -> TerminalCommand:
        self.command = command
        return command

    def get_command(self, command_id: str) -> TerminalCommand | None:
        return self.command if command_id == self.command.command_id else None

    def list_commands_by_terminal(self, terminal_id: str, *, limit: int = 100):
        return [self.command]

    def append_output_chunk(self, chunk: TerminalOutputChunk) -> TerminalOutputChunk:
        self.chunks.append(chunk)
        return chunk

    def next_output_chunk_index(self, terminal_id: str, command_id: str) -> int:
        return len(self.chunks)

    def list_output_chunks(
        self,
        terminal_id: str,
        *,
        command_id: str | None = None,
        after_chunk_index: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[TerminalOutputChunk]:
        self.list_threads.append(threading.current_thread().name)
        chunks = [chunk for chunk in self.chunks if chunk.terminal_id == terminal_id]
        if command_id is not None:
            chunks = [chunk for chunk in chunks if chunk.command_id == command_id]
        if after_chunk_index is not None:
            chunks = [chunk for chunk in chunks if chunk.chunk_index > after_chunk_index]
        return chunks[offset: offset + limit]

    def search_output_chunks(
        self,
        terminal_id: str,
        query: str,
        *,
        command_id: str | None = None,
        case_sensitive: bool = False,
        limit: int = 100,
    ) -> list[TerminalOutputChunk]:
        self.search_threads.append(threading.current_thread().name)
        chunks = [chunk for chunk in self.chunks if chunk.terminal_id == terminal_id]
        if command_id is not None:
            chunks = [chunk for chunk in chunks if chunk.command_id == command_id]
        return chunks[:limit]


class _UnusedProcessManager:
    async def start_terminal(
        self,
        request: TerminalSessionStartRequest,
        *,
        on_exit,
        on_shell_ready=None,
        on_command_finished=None,
    ):
        return True

    async def start_command(self, request: TerminalProcessStartRequest, *, on_output, on_exit) -> None:
        return None

    async def write_input(self, terminal_id: str, text: str) -> int:
        return len(text)

    async def interrupt_terminal(self, terminal_id: str) -> None:
        return None

    async def terminate_terminal(self, terminal_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _service(repo: _FakeTerminalRepository) -> TerminalService:
    return TerminalService(
        repository=repo,
        process_manager=_UnusedProcessManager(),
        scope_resolver=TerminalRuntimeScopeResolver(),
        max_output_chars_per_read=50000,
    )


def _scope() -> TerminalRuntimeScope:
    return TerminalRuntimeScope(owner_type="task", owner_id="task-1", default_cwd="D:/workspace")


def test_read_output_offloads_since_page_and_tail_repo_queries_to_db_pool() -> None:
    configure_blocking_pool(
        BlockingPool.DB_BLOCKING,
        max_workers=2,
        isolated=True,
        thread_name_prefix="test-terminal-db",
        max_pending=8,
    )
    repo = _FakeTerminalRepository()
    service = _service(repo)

    async def run_case() -> None:
        since = await service.read_output(
            _scope(),
            terminal_id="term-1",
            command_id="cmd-1",
            mode="since",
            after_chunk_index=0,
            limit_lines=10,
        )
        page = await service.read_output(
            _scope(),
            terminal_id="term-1",
            command_id="cmd-1",
            mode="page",
            offset=1,
            limit_lines=1,
        )
        tail = await service.read_output(
            _scope(),
            terminal_id="term-1",
            command_id="cmd-1",
            mode="tail",
            tail_lines=1,
            limit_lines=10,
        )

        assert since.start_chunk_index == 1
        assert since.next_chunk_index == 3
        assert page.start_chunk_index == 1
        assert page.chunks_returned == 1
        assert tail.text.strip().endswith("last line")

    try:
        asyncio.run(run_case())
    finally:
        shutdown_blocking_executors(wait=True, cancel_futures=True)

    assert repo.list_threads
    assert all(thread_name.startswith("test-terminal-db") for thread_name in repo.list_threads)


def test_read_output_search_offload_diagnostics_do_not_log_query(caplog: pytest.LogCaptureFixture) -> None:
    configure_blocking_pool(
        BlockingPool.DB_BLOCKING,
        max_workers=1,
        isolated=True,
        thread_name_prefix="test-terminal-search",
        max_pending=4,
    )
    repo = _FakeTerminalRepository()
    service = _service(repo)

    async def run_case():
        with caplog.at_level(logging.DEBUG, logger="agent_runtime.terminal.terminal_service"):
            with caplog.at_level(logging.DEBUG, logger="agent_runtime.infrastructure.asyncio_diagnostics"):
                return await service.read_output(
                    _scope(),
                    terminal_id="term-1",
                    command_id="cmd-1",
                    mode="search",
                    query="super-secret-query",
                    case_sensitive=True,
                    limit_lines=2,
                )

    try:
        result = asyncio.run(run_case())
    finally:
        shutdown_blocking_executors(wait=True, cancel_futures=True)

    assert result.chunks_returned == 2
    assert repo.search_threads
    assert all(thread_name.startswith("test-terminal-search") for thread_name in repo.search_threads)
    assert "query_length=18" in caplog.text
    assert "super-secret-query" not in caplog.text
