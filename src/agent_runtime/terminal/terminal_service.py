from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    PublishCommunicationMessageRequest,
)
from agent_runtime.infrastructure.asyncio_diagnostics import (
    BlockingPool,
    BlockingPoolOverloadedError,
    monitored_to_thread,
)
from agent_runtime.terminal.terminal import (
    TERMINAL_STATUS_BUSY,
    TERMINAL_STATUS_DELETED,
    TERMINAL_STATUS_ERROR,
    TERMINAL_STATUS_IDLE,
    TerminalInstance,
)
from agent_runtime.terminal.terminal_command import (
    TERMINAL_COMMAND_STATUS_CANCELLED,
    TERMINAL_COMMAND_STATUS_COMPLETED,
    TERMINAL_COMMAND_STATUS_FAILED,
    TERMINAL_COMMAND_STATUS_TIMEOUT,
    TerminalCommand,
)
from agent_runtime.terminal.shell_policy import platform_supported_shell, resolve_supported_shell
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError
from agent_runtime.terminal.terminal_output import TerminalOutputChunk
from agent_runtime.terminal.terminal_output_ring import TerminalOutputRing
from agent_runtime.terminal.terminal_process import TerminalProcessManager, TerminalProcessStartRequest, TerminalSessionStartRequest
from agent_runtime.terminal.terminal_repository import TerminalRepository
from agent_runtime.terminal.terminal_rendering import render_terminal_chunks

ReadMode = Literal["tail", "page", "since", "search"]
MAX_TERMINAL_READ_WAIT_TIMEOUT_MS = 600_000
TERMINAL_COMMAND_FINISHED_STATUSES = frozenset({
    TERMINAL_COMMAND_STATUS_COMPLETED,
    TERMINAL_COMMAND_STATUS_FAILED,
    TERMINAL_COMMAND_STATUS_CANCELLED,
    TERMINAL_COMMAND_STATUS_TIMEOUT,
})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TerminalRuntimeScope:
    owner_type: str
    owner_id: str
    default_cwd: str
    communication_stream_id: str | None = None
    tool_call_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    trace_id: str | None = None


@dataclass(slots=True)
class _PendingTerminalOutput:
    scope: TerminalRuntimeScope
    terminal_id: str
    command_id: str
    stream: str
    parts: list[str]
    byte_offset: int
    chunk_count: int = 0
    pending_bytes: int = 0
    flush_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class ExecuteCommandAccepted:
    accepted: bool
    terminal: TerminalInstance
    command: TerminalCommand


@dataclass(frozen=True, slots=True)
class TerminalOutputReadResult:
    terminal_id: str
    command_id: str | None
    command_status: str | None
    command_exit_code: int | None
    command_completed_at: str | None
    command_finished: bool | None
    status: str
    text: str
    chunks_returned: int
    truncated: bool
    start_chunk_index: int | None
    end_chunk_index: int | None
    next_chunk_index: int | None
    has_more: bool


class TerminalRuntimeScopeResolver:
    def __init__(
        self,
        *,
        agent_runtime_gateway: AgentRuntimeGateway | None = None,
        communication_gateway: CommunicationGateway | None = None,
    ) -> None:
        self._agent_runtime_gateway = agent_runtime_gateway
        self._communication_gateway = communication_gateway

    def resolve(
        self,
        *,
        scene_id: str | None = None,
        task_id: str | None = None,
        agent_instance_id: str | None = None,
        communication_stream_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        workspace_path: str | None = None,
    ) -> TerminalRuntimeScope:
        actual_scene_id: str | None = scene_id
        default_cwd: str | None = workspace_path
        actual_agent_instance_id = _string_or_none(agent_instance_id)
        if actual_agent_instance_id is None:
            raise ValueError("终端操作必须显式提供非空 agent_instance_id，禁止从 task、scene 或 stream 猜测归属。")

        if self._agent_runtime_gateway is not None and task_id:
            task = self._agent_runtime_gateway.get_task(task_id)
            actual_scene_id = actual_scene_id or task.scene_id
        if self._agent_runtime_gateway is not None:
            instance = self._agent_runtime_gateway.get_agent_instance(actual_agent_instance_id)
            default_cwd = default_cwd or instance.current_working_directory
            trace_id = trace_id or instance.trace_id
            task_id = task_id or instance.task_id
            if actual_scene_id is None:
                try:
                    task = self._agent_runtime_gateway.get_task(instance.task_id)
                    actual_scene_id = task.scene_id
                except KeyError:
                    pass

        stream_metadata: dict[str, Any] = {}
        if self._communication_gateway is not None and communication_stream_id:
            try:
                stream_metadata = dict(self._communication_gateway.get_stream(communication_stream_id).metadata)
            except KeyError:
                stream_metadata = {}
        actual_scene_id = actual_scene_id or _string_or_none(stream_metadata.get("scene_id"))
        default_cwd = default_cwd or _string_or_none(stream_metadata.get("workspace_path"))

        if actual_scene_id:
            owner_type = "scene"
            owner_id = actual_scene_id
        elif task_id:
            owner_type = "task"
            owner_id = task_id
        elif communication_stream_id:
            owner_type = "communication_stream"
            owner_id = communication_stream_id
        else:
            raise ValueError("终端工具需要可解析的 task_id、agent_instance_id 或 communication_stream_id。")

        if not default_cwd:
            default_cwd = os.getcwd()

        return TerminalRuntimeScope(
            owner_type=owner_type,
            owner_id=owner_id,
            default_cwd=str(Path(default_cwd).expanduser()),
            communication_stream_id=communication_stream_id,
            tool_call_id=request_id,
            task_id=task_id,
            agent_instance_id=actual_agent_instance_id,
            trace_id=trace_id,
        )


class TerminalService:
    def __init__(
        self,
        *,
        repository: TerminalRepository,
        process_manager: TerminalProcessManager,
        scope_resolver: TerminalRuntimeScopeResolver,
        communication_gateway: CommunicationGateway | None = None,
        default_shell: str | None = None,
        max_output_chars_per_read: int = 100_000,
        max_live_terminals: int = 200,
        max_live_terminals_per_agent: int = 16,
        output_coalesce_interval_ms: int = 50,
        output_coalesce_max_chars: int | None = None,
        output_coalesce_max_chunks: int = 32,
        output_coalesce_max_bytes: int | None = None,
        output_ring_max_chunks: int = 1000,
        output_ring_max_chars: int = 100_000,
    ) -> None:
        self._repo = repository
        self._process_manager = process_manager
        self._scope_resolver = scope_resolver
        self._communication_gateway = communication_gateway
        self._default_shell = default_shell or _default_shell_name()
        self._max_output_chars_per_read = max_output_chars_per_read
        self._max_live_terminals = max(1, max_live_terminals)
        self._max_live_terminals_per_agent = max(1, min(max_live_terminals_per_agent, self._max_live_terminals))
        self._output_coalesce_interval_ms = max(0, output_coalesce_interval_ms)
        # 优先 max_chars；兼容旧 max_bytes 配置。
        coalesce_chars = (
            output_coalesce_max_chars
            if output_coalesce_max_chars is not None
            else output_coalesce_max_bytes
            if output_coalesce_max_bytes is not None
            else 16_384
        )
        self._output_coalesce_max_chars = max(1, int(coalesce_chars))
        self._output_coalesce_max_chunks = max(1, int(output_coalesce_max_chunks))
        self._output_ring = TerminalOutputRing(
            max_chunks=max(1, output_ring_max_chunks),
            max_chars=max(1000, output_ring_max_chars),
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._byte_offsets: dict[tuple[str, str], int] = {}
        self._stream_bytes: dict[tuple[str, str, str], int] = {}
        self._runtime_tokens: dict[str, str] = {}
        # terminal_id -> agent_instance_id for currently mounted live runtimes
        self._live_slots: dict[str, str | None] = {}
        self._capacity_lock = asyncio.Lock()
        self._pending_outputs: dict[tuple[str, str, str], _PendingTerminalOutput] = {}
        self._command_finish_events: dict[str, asyncio.Event] = {}
        self._command_output_events: dict[str, asyncio.Event] = {}
        self._command_output_versions: dict[str, int] = {}
        # terminal_id -> active/awaiting wait-state command ids (at most one in normal flow)
        self._terminal_wait_commands: dict[str, set[str]] = {}
        # (terminal_id, command_id) -> in-flight flush futures used as completion barriers
        self._inflight_output_flushes: dict[tuple[str, str], set[asyncio.Future[None]]] = {}

    def resolve_scope(
        self,
        *,
        scene_id: str | None = None,
        task_id: str | None = None,
        agent_instance_id: str | None = None,
        communication_stream_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        workspace_path: str | None = None,
    ) -> TerminalRuntimeScope:
        return self._scope_resolver.resolve(
            scene_id=scene_id,
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            communication_stream_id=communication_stream_id,
            request_id=request_id,
            trace_id=trace_id,
            workspace_path=workspace_path,
        )

    async def create_terminal(
        self,
        scope: TerminalRuntimeScope,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        terminal_id: str | None = None,
    ) -> TerminalInstance:
        started_at = time.perf_counter()
        logger.info(
            "Terminal create requested: owner_type=%s owner_id=%s requested_terminal_id=%s requested_cwd=%s requested_shell=%s task_id=%s agent_instance_id=%s trace_id=%s",
            scope.owner_type,
            scope.owner_id,
            terminal_id,
            cwd,
            shell,
            scope.task_id,
            scope.agent_instance_id,
            scope.trace_id,
        )
        if terminal_id:
            existing = self._repo.get_terminal(terminal_id)
            if existing is not None and existing.status == TERMINAL_STATUS_DELETED:
                raise KeyError(f"终端已删除，不能复用: {terminal_id}")
        actual_cwd = self._resolve_cwd(cwd, scope.default_cwd)
        actual_shell = self._resolve_shell(shell)
        terminal = TerminalInstance(
            terminal_id=terminal_id or _new_id("term"),
            owner_type=scope.owner_type,
            owner_id=scope.owner_id,
            cwd=actual_cwd,
            shell=actual_shell,
            metadata={
                "task_id": scope.task_id,
                "agent_instance_id": scope.agent_instance_id,
                "communication_stream_id": scope.communication_stream_id,
                "trace_id": scope.trace_id,
            },
        )
        await self._ensure_terminal_runtime(scope, terminal, cwd=terminal.cwd or actual_cwd, shell=terminal.shell)
        terminal = self._repo.get_terminal(terminal.terminal_id) or self._repo.save_terminal(terminal)
        await self._publish(
            scope,
            "terminal.created",
            {
                "terminal_id": terminal.terminal_id,
                "cwd": terminal.cwd,
                "shell": terminal.shell,
                "status": terminal.status,
                "created_at": terminal.created_at.isoformat() if terminal.created_at else None,
            },
            correlation_id=scope.tool_call_id,
        )
        logger.info(
            "Terminal create completed: terminal_id=%s cwd=%s shell=%s status=%s task_id=%s agent_instance_id=%s duration_ms=%s",
            terminal.terminal_id,
            terminal.cwd,
            terminal.shell,
            terminal.status,
            scope.task_id,
            scope.agent_instance_id,
            int((time.perf_counter() - started_at) * 1000),
        )
        return terminal

    def list_terminals(self, scope: TerminalRuntimeScope) -> list[TerminalInstance]:
        return [
            terminal
            for terminal in self._repo.list_terminals_by_owner(scope.owner_type, scope.owner_id)
            if _string_or_none((terminal.metadata or {}).get("agent_instance_id")) == scope.agent_instance_id
        ]

    def get_latest_command(self, terminal_id: str) -> TerminalCommand | None:
        commands = self._repo.list_commands_by_terminal(terminal_id, limit=1)
        return commands[0] if commands else None

    async def execute_command(
        self,
        scope: TerminalRuntimeScope,
        *,
        command_line: str,
        terminal_id: str | None = None,
        cwd: str | None = None,
        shell: str | None = None,
        timeout_ms: int | None = None,
    ) -> ExecuteCommandAccepted:
        started_at = time.perf_counter()
        logger.info(
            "Terminal execute requested: owner_type=%s owner_id=%s requested_terminal_id=%s requested_cwd=%s requested_shell=%s timeout_ms=%s command_chars=%s task_id=%s agent_instance_id=%s trace_id=%s",
            scope.owner_type,
            scope.owner_id,
            terminal_id,
            cwd,
            shell,
            timeout_ms,
            len(command_line),
            scope.task_id,
            scope.agent_instance_id,
            scope.trace_id,
        )
        lock_key = f"{scope.owner_type}:{scope.owner_id}"
        async with self._lock_for(lock_key):
            terminal = await self._select_terminal(scope, terminal_id=terminal_id, cwd=cwd, shell=shell)
            if not terminal.is_available:
                raise ValueError(
                    f"终端当前不可执行新命令: terminal_id={terminal.terminal_id} status={terminal.status} "
                    f"current_command_id={terminal.current_command_id}"
                )
            actual_cwd = self._resolve_cwd(terminal.cwd, scope.default_cwd)
            actual_shell = self._resolve_shell(terminal.shell)
            await self._ensure_terminal_runtime(scope, terminal, cwd=actual_cwd, shell=actual_shell)
            terminal = self._repo.get_terminal(terminal.terminal_id) or terminal
            if not terminal.is_available:
                raise ValueError(
                    f"终端在挂载 runtime 后仍不可执行新命令: terminal_id={terminal.terminal_id} status={terminal.status}"
                )
            command = TerminalCommand(
                command_id=_new_id("cmd"),
                terminal_id=terminal.terminal_id,
                command_line=command_line,
                requested_cwd=cwd,
                actual_cwd=actual_cwd,
                shell=actual_shell,
                tool_call_id=scope.tool_call_id,
                agent_instance_id=scope.agent_instance_id,
                trace_id=scope.trace_id,
            ).mark_running()
            terminal = terminal.mark_busy(command.command_id, cwd=actual_cwd)
            self._repo.save_terminal(terminal)
            self._repo.save_command(command)
            self._register_command_wait_state(terminal.terminal_id, command.command_id)
            self._byte_offsets[(terminal.terminal_id, command.command_id)] = 0
            self._stream_bytes[(terminal.terminal_id, command.command_id, "stdout")] = 0
            self._stream_bytes[(terminal.terminal_id, command.command_id, "stderr")] = 0

        await self._publish(
            scope,
            "terminal.session.started",
            {
                "terminal_session_id": command.command_id,
                "terminal_id": terminal.terminal_id,
                "command_id": command.command_id,
                "tool_call_id": scope.tool_call_id or command.command_id,
                "command_line": command.command_line,
                "actual_cwd": command.actual_cwd,
                "cwd": command.actual_cwd,
                "shell": command.shell,
                "started_at": command.started_at.isoformat() if command.started_at else None,
            },
            correlation_id=scope.tool_call_id,
        )

        try:
            await self._process_manager.start_command(
                TerminalProcessStartRequest(
                    terminal_id=terminal.terminal_id,
                    command_id=command.command_id,
                    command_line=command.command_line,
                    cwd=command.actual_cwd,
                    shell=command.shell,
                    timeout_ms=timeout_ms,
                ),
                on_output=lambda stream, text: self._handle_output(scope, terminal.terminal_id, command.command_id, stream, text),
                on_exit=lambda exit_code, error_message: self._handle_exit(scope, terminal.terminal_id, command.command_id, exit_code, error_message),
            )
            # Keep terminal busy until shell integration reports command finished.
            logger.info(
                "Terminal execute accepted: terminal_id=%s command_id=%s actual_cwd=%s shell=%s timeout_ms=%s task_id=%s agent_instance_id=%s duration_ms=%s",
                terminal.terminal_id,
                command.command_id,
                command.actual_cwd,
                command.shell,
                timeout_ms,
                scope.task_id,
                scope.agent_instance_id,
                int((time.perf_counter() - started_at) * 1000),
            )
        except asyncio.CancelledError:
            await self._finalize_failed_execute(
                scope,
                terminal,
                command,
                error_message="command start cancelled",
                finalizer="cancel",
                definite_no_write=False,
                started_at=started_at,
            )
            raise
        except Exception as exc:
            await self._finalize_failed_execute(
                scope,
                terminal,
                command,
                error_message=str(exc),
                finalizer="fail",
                definite_no_write=_is_definite_unwritten_command_error(exc),
                started_at=started_at,
                error_type=exc.__class__.__name__,
            )
            raise
        return ExecuteCommandAccepted(accepted=True, terminal=terminal, command=command)

    async def read_output(
        self,
        scope: TerminalRuntimeScope,
        *,
        terminal_id: str,
        command_id: str | None = None,
        mode: ReadMode = "tail",
        tail_lines: int = 80,
        offset: int = 0,
        limit_lines: int = 200,
        limit_chars: int = 12000,
        after_chunk_index: int | None = None,
        wait_timeout_ms: int = 0,
        query: str | None = None,
        case_sensitive: bool = False,
    ) -> TerminalOutputReadResult:
        started_at = time.perf_counter()
        effective_wait_timeout_ms = min(max(wait_timeout_ms, 0), MAX_TERMINAL_READ_WAIT_TIMEOUT_MS)
        logger.info(
            "读取终端输出开始: terminal_id=%s, command_id=%s, mode=%s, wait_timeout_ms=%s, effective_wait_timeout_ms=%s, limit_lines=%s, limit_chars=%s",
            terminal_id,
            command_id,
            mode,
            wait_timeout_ms,
            effective_wait_timeout_ms,
            limit_lines,
            limit_chars,
        )
        terminal = self._require_terminal(scope, terminal_id)
        read_command = self._get_read_command(terminal_id, command_id)
        effective_limit_chars = min(max(limit_chars, 1000), self._max_output_chars_per_read)
        if mode == "search" and not query:
            raise ValueError("search 模式必须提供 query。")
        if effective_wait_timeout_ms > 0 and command_id is not None:
            await self._wait_for_read(
                terminal_id=terminal_id,
                command_id=command_id,
                mode=mode,
                after_chunk_index=after_chunk_index,
                wait_timeout_ms=effective_wait_timeout_ms,
            )
            terminal = self._require_terminal(scope, terminal_id)
            read_command = self._get_read_command(terminal_id, command_id)
        elif effective_wait_timeout_ms > 0 and command_id is None:
            # No command association: never blind-sleep; return immediate snapshot.
            pass
        result: TerminalOutputReadResult
        if mode == "search":
            chunks = await self._read_output_repo_search(
                terminal_id=terminal_id,
                command_id=command_id,
                query=query,
                case_sensitive=case_sensitive,
                limit=limit_lines,
                mode=mode,
            )
        elif mode == "since":
            chunks = await self._read_output_repo_list(
                terminal_id=terminal_id,
                command_id=command_id,
                after_chunk_index=after_chunk_index,
                limit=limit_lines,
                mode=mode,
                operation="terminal.read_output.repo_list",
            )
        elif mode == "page":
            chunks = await self._read_output_repo_list(
                terminal_id=terminal_id,
                command_id=command_id,
                offset=offset,
                limit=limit_lines,
                mode=mode,
                operation="terminal.read_output.repo_list",
            )
        else:
            terminal_chunks = await self._read_output_repo_list(
                terminal_id=terminal_id,
                limit=5000,
                mode=mode,
                tail_lines=tail_lines,
                operation="terminal.read_output.repo_tail_terminal_chunks",
            )
            command_chunks = (
                await self._read_output_repo_list(
                    terminal_id=terminal_id,
                    command_id=command_id,
                    limit=5000,
                    mode=mode,
                    tail_lines=tail_lines,
                    operation="terminal.read_output.repo_tail_command_chunks",
                )
                if command_id is not None
                else terminal_chunks
            )
            rendered = await monitored_to_thread(
                render_terminal_chunks,
                [chunk.chunk for chunk in terminal_chunks],
                operation="terminal.read_output.render_tail",
                caller="TerminalService.read_output",
                pool=BlockingPool.TERMINAL_RENDER,
                log=logger,
                diagnostic_context={
                    "terminal_id": terminal_id,
                    "command_id": command_id,
                    "mode": mode,
                    "chunk_count": len(terminal_chunks),
                    "tail_lines": tail_lines,
                },
            )
            lines = rendered.text.splitlines(keepends=True)
            tail_text = "".join(lines[-max(1, tail_lines):])
            result = _build_read_result(
                terminal_id=terminal_id,
                command_id=command_id,
                command=read_command,
                status=terminal.status,
                chunks=command_chunks,
                text=tail_text,
                limit_chars=effective_limit_chars,
                has_more=rendered.line_count > tail_lines,
            )
            logger.info(
                "读取终端输出完成: terminal_id=%s, command_id=%s, mode=%s, chunks_returned=%s, text_chars=%s, truncated=%s, has_more=%s, status=%s, 耗时=%sms",
                terminal_id,
                command_id,
                mode,
                result.chunks_returned,
                len(result.text),
                result.truncated,
                result.has_more,
                result.status,
                int((time.perf_counter() - started_at) * 1000),
            )
            return result
        rendered = await monitored_to_thread(
            render_terminal_chunks,
            [chunk.chunk for chunk in chunks],
            operation="terminal.read_output.render",
            caller="TerminalService.read_output",
            pool=BlockingPool.TERMINAL_RENDER,
            log=logger,
            diagnostic_context={
                "terminal_id": terminal_id,
                "command_id": command_id,
                "mode": mode,
                "chunk_count": len(chunks),
                "limit_lines": limit_lines,
            },
        )
        result = _build_read_result(
            terminal_id=terminal_id,
            command_id=command_id,
            command=read_command,
            status=terminal.status,
            chunks=chunks,
            text=rendered.text,
            limit_chars=effective_limit_chars,
            has_more=len(chunks) >= limit_lines,
        )
        logger.info(
            "读取终端输出完成: terminal_id=%s, command_id=%s, mode=%s, chunks_returned=%s, text_chars=%s, truncated=%s, has_more=%s, status=%s, 耗时=%sms",
            terminal_id,
            command_id,
            mode,
            result.chunks_returned,
            len(result.text),
            result.truncated,
            result.has_more,
            result.status,
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def _read_output_repo_list(
        self,
        *,
        terminal_id: str,
        command_id: str | None = None,
        after_chunk_index: int | None = None,
        offset: int = 0,
        limit: int = 500,
        mode: ReadMode,
        operation: str,
        tail_lines: int | None = None,
    ) -> list[TerminalOutputChunk]:
        return await monitored_to_thread(
            self._repo.list_output_chunks,
            terminal_id,
            command_id=command_id,
            after_chunk_index=after_chunk_index,
            offset=offset,
            limit=limit,
            operation=operation,
            caller="TerminalService.read_output",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={
                "terminal_id": terminal_id,
                "command_id": command_id,
                "mode": mode,
                "limit_lines": limit,
                "tail_lines": tail_lines,
                "after_chunk_index": after_chunk_index,
                "offset": offset,
            },
        )

    async def _read_output_repo_search(
        self,
        *,
        terminal_id: str,
        command_id: str | None,
        query: str,
        case_sensitive: bool,
        limit: int,
        mode: ReadMode,
    ) -> list[TerminalOutputChunk]:
        return await monitored_to_thread(
            self._repo.search_output_chunks,
            terminal_id,
            query,
            command_id=command_id,
            case_sensitive=case_sensitive,
            limit=limit,
            operation="terminal.read_output.repo_search",
            caller="TerminalService.read_output",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={
                "terminal_id": terminal_id,
                "command_id": command_id,
                "mode": mode,
                "limit_lines": limit,
                "case_sensitive": case_sensitive,
                "has_query": True,
                "query_length": len(query),
            },
        )

    async def write_input(
        self,
        scope: TerminalRuntimeScope,
        *,
        terminal_id: str,
        text: str | None = None,
        append_newline: bool = False,
        control: str | None = None,
    ) -> int:
        terminal = self._require_usable_terminal(scope, terminal_id)
        try:
            if control == "ctrl_c":
                await self._process_manager.interrupt_terminal(terminal_id)
                bytes_written = 0
            else:
                payload = _control_text(control) if control else (text or "")
                if append_newline:
                    payload += "\n"
                if not payload:
                    raise ValueError("写入终端的输入不能为空。")
                bytes_written = await self._process_manager.write_input(terminal_id, payload)
        except KeyError as exc:
            raise ValueError(f"终端运行进程不可写，可能命令已结束或运行时已重启: {terminal_id}") from exc
        await self._publish(
            scope,
            "terminal.input.sent",
            {
                "terminal_id": terminal_id,
                "bytes_written": bytes_written,
                "control": control,
            },
            correlation_id=scope.tool_call_id,
        )
        return bytes_written

    async def close_terminal(self, scope: TerminalRuntimeScope, terminal_id: str) -> TerminalInstance:
        terminal = self._require_usable_terminal(scope, terminal_id)
        await self._flush_terminal_outputs(terminal_id)
        # Keep runtime_token until terminate so watcher on_exit is not treated as stale.
        runtime_token_before_close = self._runtime_tokens.get(terminal_id)
        watcher_handled_detach = False
        try:
            await self._process_manager.terminate_terminal(terminal_id)
        finally:
            watcher_handled_detach = (
                runtime_token_before_close is not None
                and self._runtime_tokens.get(terminal_id) != runtime_token_before_close
            )
            self._runtime_tokens.pop(terminal_id, None)
            await self._release_live_slot(terminal_id)
            self._wake_and_cleanup_terminal_wait_states(terminal_id)
            self._discard_terminal_runtime_state(terminal_id)
        detached = terminal.mark_idle()
        await self._repo_save_terminal(detached)
        if not watcher_handled_detach:
            await self._publish(
                scope,
                "terminal.runtime.detached",
                {
                    "terminal_id": terminal_id,
                    "status": detached.status,
                },
                correlation_id=scope.tool_call_id,
            )
        return detached

    async def delete_terminal(self, scope: TerminalRuntimeScope, terminal_id: str) -> TerminalInstance:
        terminal = self._require_terminal(scope, terminal_id)
        await self._flush_terminal_outputs(terminal_id)
        await self._process_manager.terminate_terminal(terminal_id)
        self._runtime_tokens.pop(terminal_id, None)
        await self._release_live_slot(terminal_id)
        self._discard_terminal_runtime_state(terminal_id)
        deleted = terminal.delete()
        self._repo.save_terminal(deleted)
        self._repo.delete_terminal_data(terminal_id)
        await self._publish(
            scope,
            "terminal.deleted",
            {
                "terminal_id": terminal_id,
                "status": deleted.status,
            },
            correlation_id=scope.tool_call_id,
        )
        return deleted

    async def delete_task_terminals(self, task_id: str) -> list[TerminalInstance]:
        terminals = self._repo.list_terminals_by_task_id(task_id)
        deleted_terminals: list[TerminalInstance] = []
        for terminal in terminals:
            try:
                await self._flush_terminal_outputs(terminal.terminal_id)
                await self._process_manager.terminate_terminal(terminal.terminal_id)
            except Exception:
                logger.exception("关闭任务终端运行时失败: task_id=%s terminal_id=%s", task_id, terminal.terminal_id)
            self._runtime_tokens.pop(terminal.terminal_id, None)
            await self._release_live_slot(terminal.terminal_id)
            self._discard_terminal_runtime_state(terminal.terminal_id)
            deleted = terminal.delete()
            self._repo.save_terminal(deleted)
            self._repo.delete_terminal_data(terminal.terminal_id)
            deleted_terminals.append(deleted)
            scope = self._scope_for_existing_terminal(deleted)
            await self._publish(
                scope,
                "terminal.deleted",
                {
                    "terminal_id": deleted.terminal_id,
                    "status": deleted.status,
                },
                correlation_id=scope.tool_call_id,
            )
        return deleted_terminals

    async def shutdown(self) -> None:
        await self._process_manager.shutdown()

    async def _select_terminal(
        self,
        scope: TerminalRuntimeScope,
        *,
        terminal_id: str | None,
        cwd: str | None,
        shell: str | None,
    ) -> TerminalInstance:
        if terminal_id:
            terminal = self._require_usable_terminal(scope, terminal_id)
            terminal = self._recover_terminal_slot_if_needed(terminal)
            if cwd is not None:
                requested_cwd = self._resolve_cwd(cwd, scope.default_cwd)
                current_cwd = self._resolve_cwd(terminal.cwd, scope.default_cwd)
                if requested_cwd != current_cwd:
                    raise ValueError(f"终端 {terminal.terminal_id} 的 cwd 固定为 {current_cwd}，不能在执行命令时改为 {requested_cwd}。")
            if shell is not None:
                requested_shell = self._resolve_shell(shell)
                current_shell = self._resolve_shell(terminal.shell)
                if requested_shell != current_shell:
                    raise ValueError(f"终端 {terminal.terminal_id} 的 shell 固定为 {current_shell}，不能在执行命令时改为 {requested_shell}。")
            return terminal
        requested_cwd = self._resolve_cwd(cwd, scope.default_cwd) if cwd is not None else None
        requested_shell = self._resolve_shell(shell) if shell is not None else None
        for terminal in self._repo.list_terminals_by_owner(scope.owner_type, scope.owner_id):
            if _string_or_none((terminal.metadata or {}).get("agent_instance_id")) != scope.agent_instance_id:
                continue
            if terminal.status not in {TERMINAL_STATUS_IDLE, TERMINAL_STATUS_ERROR}:
                continue
            if requested_cwd is not None and self._resolve_cwd(terminal.cwd, scope.default_cwd) != requested_cwd:
                continue
            if requested_shell is not None and self._resolve_shell(terminal.shell) != requested_shell:
                continue
            return self._recover_terminal_slot_if_needed(terminal)
        return await self.create_terminal(scope, cwd=cwd, shell=shell)

    def _require_terminal(self, scope: TerminalRuntimeScope, terminal_id: str) -> TerminalInstance:
        terminal = self._repo.get_terminal(terminal_id)
        terminal_agent_instance_id = (
            _string_or_none((terminal.metadata or {}).get("agent_instance_id"))
            if terminal is not None
            else None
        )
        if (
            terminal is None
            or terminal.owner_type != scope.owner_type
            or terminal.owner_id != scope.owner_id
            or terminal_agent_instance_id != scope.agent_instance_id
        ):
            raise KeyError(f"终端不存在: {terminal_id}")
        return terminal

    def _require_usable_terminal(self, scope: TerminalRuntimeScope, terminal_id: str) -> TerminalInstance:
        terminal = self._require_terminal(scope, terminal_id)
        if terminal.status == TERMINAL_STATUS_DELETED:
            raise KeyError(f"终端已删除: {terminal_id}")
        return terminal

    def _scope_for_existing_terminal(self, terminal: TerminalInstance) -> TerminalRuntimeScope:
        metadata = terminal.metadata or {}
        return TerminalRuntimeScope(
            owner_type=terminal.owner_type,
            owner_id=terminal.owner_id,
            default_cwd=terminal.cwd or os.getcwd(),
            communication_stream_id=_string_or_none(metadata.get("communication_stream_id")),
            task_id=_string_or_none(metadata.get("task_id")),
            agent_instance_id=_string_or_none(metadata.get("agent_instance_id")),
            trace_id=_string_or_none(metadata.get("trace_id")),
        )

    def _recover_terminal_slot_if_needed(self, terminal: TerminalInstance) -> TerminalInstance:
        if terminal.status != TERMINAL_STATUS_ERROR:
            return terminal
        recovered = terminal.mark_idle()
        self._repo.save_terminal(recovered)
        return recovered

    async def _ensure_terminal_runtime(
        self,
        scope: TerminalRuntimeScope,
        terminal: TerminalInstance,
        *,
        cwd: str,
        shell: str,
    ) -> None:
        runtime_token = _new_id("runtime")
        # Phase 1: reserve capacity quickly without waiting on shell bootstrap.
        async with self._capacity_lock:
            already_live = terminal.terminal_id in self._live_slots
            if not already_live:
                self._assert_can_admit_live(scope.agent_instance_id)
                self._live_slots[terminal.terminal_id] = scope.agent_instance_id
        # Phase 2: spawn/wait READY outside capacity lock.
        try:
            started = await self._process_manager.start_terminal(
                TerminalSessionStartRequest(
                    terminal_id=terminal.terminal_id,
                    cwd=cwd,
                    shell=shell,
                    runtime_token=runtime_token,
                ),
                on_exit=lambda exit_code, error_message: self._handle_terminal_exit(
                    scope,
                    terminal.terminal_id,
                    runtime_token,
                    exit_code,
                    error_message,
                ),
                on_shell_ready=lambda terminal_id: self._handle_shell_ready(scope, terminal_id),
                on_command_finished=lambda command_id, exit_code, error_message, final_status: self._handle_exit(
                    scope, terminal.terminal_id, command_id, exit_code, error_message, final_status
                ),
            )
        except Exception:
            if not already_live:
                async with self._capacity_lock:
                    self._live_slots.pop(terminal.terminal_id, None)
            raise
        async with self._capacity_lock:
            if already_live and started:
                self._live_slots[terminal.terminal_id] = scope.agent_instance_id
            if started:
                self._runtime_tokens[terminal.terminal_id] = runtime_token

    async def _handle_output(
        self,
        scope: TerminalRuntimeScope,
        terminal_id: str,
        command_id: str,
        stream: str,
        text: str,
    ) -> None:
        if not text:
            return
        # Coalesce=0 means immediate write path for tests/low-latency configs.
        if self._output_coalesce_interval_ms <= 0:
            await self._persist_and_publish_output(
                scope,
                terminal_id=terminal_id,
                command_id=command_id,
                stream=stream,
                text=text,
                coalesced=False,
                source_chunk_count=1,
            )
            return

        pending_key = (terminal_id, command_id, stream)
        should_flush_now = False
        async with self._lock_for(f"output:{terminal_id}:{command_id}"):
            pending = self._pending_outputs.get(pending_key)
            encoded_len = len(text.encode("utf-8", errors="replace"))
            if pending is None:
                key = (terminal_id, command_id)
                pending = _PendingTerminalOutput(
                    scope=scope,
                    terminal_id=terminal_id,
                    command_id=command_id,
                    stream=stream,
                    parts=[],
                    byte_offset=self._byte_offsets.get(key, 0),
                )
                self._pending_outputs[pending_key] = pending
            pending.parts.append(text)
            pending.chunk_count += 1
            pending.pending_bytes += encoded_len
            pending_chars = sum(len(part) for part in pending.parts)
            should_flush_now = (
                pending_chars >= self._output_coalesce_max_chars
                or pending.chunk_count >= self._output_coalesce_max_chunks
            )
            if should_flush_now:
                if pending.flush_task is not None and not pending.flush_task.done():
                    pending.flush_task.cancel()
                pending.flush_task = None
            elif pending.flush_task is None or pending.flush_task.done():
                pending.flush_task = asyncio.create_task(
                    self._delayed_flush_pending_output(pending_key),
                    name=f"terminal-output-coalesce-{terminal_id}-{command_id}",
                )
        if should_flush_now:
            await self._flush_pending_output(pending_key)

    async def _handle_exit(
        self,
        scope: TerminalRuntimeScope,
        terminal_id: str,
        command_id: str,
        exit_code: int | None,
        error_message: str | None,
        final_status: str | None = None,
    ) -> None:
        # Command finished (DONE). Terminal stays busy until shell READY(B).
        await self._flush_command_outputs(terminal_id, command_id)
        command = await self._repo_get_command(command_id)
        terminal = await self._repo_get_terminal(terminal_id)
        if command is None or terminal is None:
            self._signal_command_finished(command_id)
            return
        if command.status in TERMINAL_COMMAND_FINISHED_STATUSES:
            self._signal_command_finished(command_id)
            return
        stdout_bytes = self._stream_bytes.get((terminal_id, command_id, "stdout"), 0)
        stdout_bytes += self._stream_bytes.get((terminal_id, command_id, "pty"), 0)
        stderr_bytes = self._stream_bytes.get((terminal_id, command_id, "stderr"), 0)
        status = final_status
        if status is None:
            if error_message and "interrupted by ctrl_c" in error_message:
                status = "cancelled"
            elif error_message:
                status = "failed"
            elif exit_code == 0:
                status = "completed"
            else:
                status = "failed"
        if status == "cancelled":
            completed = command.cancel(
                error_message or "command interrupted by ctrl_c",
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        elif status == "timeout":
            completed = command.timeout(
                error_message or "command timed out",
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        elif status == "failed" or (status != "completed" and error_message):
            if exit_code is not None and not error_message:
                # DONE;N with N!=0: keep real exit code via complete() failed branch.
                completed = command.complete(
                    exit_code=exit_code,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                )
            else:
                completed = command.fail(
                    error_message or f"command failed with exit_code={exit_code}",
                    exit_code=exit_code,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                )
        else:
            completed = command.complete(
                exit_code=0 if status == "completed" else (exit_code if exit_code is not None else 1),
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        await self._repo_save_command(completed)
        # NOTE: do not mark terminal idle here; READY(B) does that.
        await self._publish(
            scope,
            "terminal.session.completed",
            {
                "terminal_session_id": command_id,
                "terminal_id": terminal_id,
                "command_id": command_id,
                "tool_call_id": scope.tool_call_id or command_id,
                "exit_code": completed.exit_code,
                "duration_ms": completed.duration_ms,
                "stdout_bytes": completed.stdout_bytes,
                "stderr_bytes": completed.stderr_bytes,
                "error_message": completed.error_message,
                "completed_at": completed.completed_at.isoformat() if completed.completed_at else None,
            },
            correlation_id=scope.tool_call_id,
        )
        self._signal_command_finished(command_id)

    async def _handle_shell_ready(self, scope: TerminalRuntimeScope, terminal_id: str) -> None:
        terminal = await self._repo_get_terminal(terminal_id)
        if terminal is None or terminal.status == TERMINAL_STATUS_DELETED:
            return
        command_id = terminal.current_command_id
        # Flush D..B prompt text still bound to the command before idle/registry cleanup.
        if command_id:
            await self._flush_command_outputs(terminal_id, command_id)
        if terminal.status == TERMINAL_STATUS_BUSY:
            try:
                await self._repo_save_terminal(terminal.mark_idle())
            except ValueError:
                await self._repo_save_terminal(terminal.mark_error())
        if command_id:
            self._cleanup_command_wait_state(terminal_id, command_id)

    async def _handle_terminal_exit(
        self,
        scope: TerminalRuntimeScope,
        terminal_id: str,
        runtime_token: str | None,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        terminal = await self._repo_get_terminal(terminal_id)
        if terminal is None or terminal.status == TERMINAL_STATUS_DELETED:
            self._wake_and_cleanup_terminal_wait_states(terminal_id)
            await self._release_live_slot(terminal_id)
            return
        if runtime_token is not None and self._runtime_tokens.get(terminal_id) != runtime_token:
            return
        await self._flush_terminal_outputs(terminal_id)
        self._wake_and_cleanup_terminal_wait_states(terminal_id)
        updated = terminal.mark_error() if error_message else terminal.mark_idle()
        self._runtime_tokens.pop(terminal_id, None)
        await self._release_live_slot(terminal_id)
        await self._repo_save_terminal(updated)
        await self._publish(
            scope,
            "terminal.runtime.detached",
            {
                "terminal_id": terminal_id,
                "status": updated.status,
                "exit_code": exit_code,
                "error_message": error_message,
            },
            correlation_id=scope.tool_call_id,
        )

    async def _publish(
        self,
        scope: TerminalRuntimeScope,
        event_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> None:
        if self._communication_gateway is None or scope.communication_stream_id is None:
            return
        if _string_or_none(scope.agent_instance_id) is None:
            logger.error(
                "Terminal Agent-scoped event rejected before publish: event_type=%s terminal_id=%s task_id=%s owner_type=%s owner_id=%s",
                event_type,
                payload.get("terminal_id"),
                scope.task_id,
                scope.owner_type,
                scope.owner_id,
            )
            return
        try:
            await self._communication_gateway.publish_message(
                PublishCommunicationMessageRequest(
                    stream_id=scope.communication_stream_id,
                    message_type=event_type,
                    payload=payload,
                    source="terminal_service",
                    actor_id=scope.agent_instance_id,
                    correlation_id=correlation_id,
                    metadata={
                        "task_id": scope.task_id,
                        "agent_instance_id": scope.agent_instance_id,
                        "trace_id": scope.trace_id,
                        "owner_type": scope.owner_type,
                        "owner_id": scope.owner_id,
                    },
                )
            )
        except Exception:
            logger.exception(
                "Terminal communication event publish failed: event_type=%s terminal_id=%s task_id=%s agent_instance_id=%s",
                event_type,
                payload.get("terminal_id"),
                scope.task_id,
                scope.agent_instance_id,
            )

    def _get_read_command(self, terminal_id: str, command_id: str | None) -> TerminalCommand | None:
        if command_id is None:
            return None
        command = self._repo.get_command(command_id)
        if command is None or command.terminal_id != terminal_id:
            return None
        return command

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _discard_terminal_runtime_state(self, terminal_id: str) -> None:
        self._wake_and_cleanup_terminal_wait_states(terminal_id)
        for key in [key for key in list(self._inflight_output_flushes) if key[0] == terminal_id]:
            futures = self._inflight_output_flushes.pop(key, set())
            for fut in futures:
                if not fut.done():
                    fut.set_result(None)
        for key in [key for key in self._byte_offsets if key[0] == terminal_id]:
            self._byte_offsets.pop(key, None)
        for key in [key for key in self._stream_bytes if key[0] == terminal_id]:
            self._stream_bytes.pop(key, None)
        for key in [key for key in self._pending_outputs if key[0] == terminal_id]:
            pending = self._pending_outputs.pop(key, None)
            if pending is not None and pending.flush_task is not None and not pending.flush_task.done():
                pending.flush_task.cancel()
        lock_prefix = f"output:{terminal_id}:"
        for key in [key for key in self._locks if key.startswith(lock_prefix)]:
            self._locks.pop(key, None)
        self._output_ring.clear_terminal(terminal_id)

    def live_terminal_count(self) -> int:
        return len(self._live_slots)

    def live_terminal_count_for_agent(self, agent_instance_id: str | None) -> int:
        if not agent_instance_id:
            return 0
        return sum(1 for owner in self._live_slots.values() if owner == agent_instance_id)

    def _assert_can_admit_live(self, agent_instance_id: str | None) -> None:
        global_current = len(self._live_slots)
        if global_current >= self._max_live_terminals:
            raise TerminalCapacityExceededError(
                (
                    f"终端容量已满：Runtime 最多允许 {self._max_live_terminals} 个 live 终端，"
                    f"当前已有 {global_current} 个。"
                ),
                scope="global",
                limit=self._max_live_terminals,
                current=global_current,
                agent_instance_id=agent_instance_id,
            )
        if agent_instance_id:
            agent_current = self.live_terminal_count_for_agent(agent_instance_id)
            if agent_current >= self._max_live_terminals_per_agent:
                raise TerminalCapacityExceededError(
                    (
                        f"终端容量已满：Agent {agent_instance_id} 最多允许 "
                        f"{self._max_live_terminals_per_agent} 个 live 终端，当前已有 {agent_current} 个。"
                    ),
                    scope="agent",
                    limit=self._max_live_terminals_per_agent,
                    current=agent_current,
                    agent_instance_id=agent_instance_id,
                )

    async def _release_live_slot(self, terminal_id: str) -> None:
        async with self._capacity_lock:
            self._live_slots.pop(terminal_id, None)

    async def _delayed_flush_pending_output(self, pending_key: tuple[str, str, str]) -> None:
        try:
            await asyncio.sleep(self._output_coalesce_interval_ms / 1000)
            await self._flush_pending_output(pending_key)
        except asyncio.CancelledError:
            # Best-effort flush if cancel interrupted sleep/append after parts were staged.
            try:
                await self._flush_pending_output(pending_key)
            except Exception:
                logger.exception(
                    "Failed best-effort flush after coalesce cancel: terminal_id=%s command_id=%s stream=%s",
                    pending_key[0],
                    pending_key[1],
                    pending_key[2],
                )
            raise

    async def _flush_terminal_outputs(self, terminal_id: str) -> None:
        command_ids = {
            key[1]
            for key in list(self._pending_outputs)
            if key[0] == terminal_id
        } | {
            key[1]
            for key in list(self._inflight_output_flushes)
            if key[0] == terminal_id
        }
        for command_id in command_ids:
            await self._flush_command_outputs(terminal_id, command_id)

    async def _flush_command_outputs(self, terminal_id: str, command_id: str) -> None:
        # Drain pending buckets and wait for any in-flight DB persist barrier.
        while True:
            keys = [
                key
                for key in list(self._pending_outputs)
                if key[0] == terminal_id and key[1] == command_id
            ]
            for key in keys:
                await self._flush_pending_output(key)
            inflight = list(self._inflight_output_flushes.get((terminal_id, command_id), ()))
            if not inflight:
                # Re-check pending in case a cancelled flush reinserted parts.
                if any(
                    key[0] == terminal_id and key[1] == command_id
                    for key in self._pending_outputs
                ):
                    continue
                return
            await asyncio.wait(inflight)

    async def _flush_pending_output(self, pending_key: tuple[str, str, str]) -> None:
        command_key = (pending_key[0], pending_key[1])
        loop = asyncio.get_running_loop()
        barrier = loop.create_future()
        async with self._lock_for(f"output:{pending_key[0]}:{pending_key[1]}"):
            self._inflight_output_flushes.setdefault(command_key, set()).add(barrier)
            pending = self._pending_outputs.pop(pending_key, None)
            if pending is None or not pending.parts:
                self._complete_inflight_flush_barrier(command_key, barrier)
                return
            if pending.flush_task is not None and not pending.flush_task.done():
                if pending.flush_task is not asyncio.current_task():
                    pending.flush_task.cancel()
            text = "".join(pending.parts)
            source_chunk_count = pending.chunk_count
            stream = pending.stream
            scope = pending.scope
            terminal_id = pending.terminal_id
            command_id = pending.command_id
            byte_offset_hint = pending.byte_offset
            persist_task = asyncio.create_task(
                self._append_output_chunk(
                    terminal_id=terminal_id,
                    command_id=command_id,
                    stream=stream,
                    text=text,
                )
            )
            cancelled_during_persist = False
            persisted = False
            chunk_index = 0
            byte_offset = 0

            def restore_pending() -> None:
                pending.parts = [text]
                pending.byte_offset = byte_offset_hint
                pending.chunk_count = source_chunk_count
                pending.pending_bytes = len(text.encode("utf-8", errors="replace"))
                pending.flush_task = None
                self._pending_outputs[pending_key] = pending

            try:
                # Shield the DB task: cancelling this flush must not cancel an already-running commit.
                chunk_index, byte_offset = await asyncio.shield(persist_task)
                persisted = True
            except asyncio.CancelledError:
                cancelled_during_persist = True
                # Defer cancellation until the DB task has a definite committed/failed result.
                persisted, chunk_index, byte_offset = await self._await_persist_task_result(persist_task)
                if not persisted:
                    restore_pending()
            except BaseException:
                if not persist_task.done():
                    persist_task.cancel()
                    try:
                        await persist_task
                    except (Exception, asyncio.CancelledError):
                        pass
                restore_pending()
                raise
            finally:
                self._complete_inflight_flush_barrier(command_key, barrier)
            if cancelled_during_persist and not persisted:
                raise asyncio.CancelledError()
        await self._publish_output_delta(
            scope,
            terminal_id=terminal_id,
            command_id=command_id,
            stream=stream,
            text=text,
            chunk_index=chunk_index,
            byte_offset=byte_offset,
            coalesced=source_chunk_count > 1,
            source_chunk_count=source_chunk_count,
        )
        if cancelled_during_persist:
            raise asyncio.CancelledError()

    def _complete_inflight_flush_barrier(
        self,
        command_key: tuple[str, str],
        barrier: asyncio.Future[None],
    ) -> None:
        if not barrier.done():
            barrier.set_result(None)
        inflight = self._inflight_output_flushes.get(command_key)
        if inflight is None:
            return
        inflight.discard(barrier)
        if not inflight:
            self._inflight_output_flushes.pop(command_key, None)

    async def _await_persist_task_result(
        self,
        persist_task: asyncio.Task[tuple[int, int]],
    ) -> tuple[bool, int, int]:
        while True:
            if persist_task.cancelled():
                return False, 0, 0
            if persist_task.done():
                try:
                    chunk_index, byte_offset = persist_task.result()
                except (Exception, asyncio.CancelledError):
                    return False, 0, 0
                return True, chunk_index, byte_offset
            try:
                chunk_index, byte_offset = await asyncio.shield(persist_task)
                return True, chunk_index, byte_offset
            except asyncio.CancelledError:
                # A repeated cancellation request must not make the DB outcome ambiguous.
                continue
            except Exception:
                return False, 0, 0

    async def _finalize_failed_execute(
        self,
        scope: TerminalRuntimeScope,
        terminal: TerminalInstance,
        command: TerminalCommand,
        *,
        error_message: str,
        finalizer: str,
        definite_no_write: bool,
        started_at: float,
        error_type: str | None = None,
    ) -> None:
        if finalizer == "cancel":
            completed = command.cancel(error_message)
        else:
            completed = command.fail(error_message)
        await self._repo_save_command(completed)
        if definite_no_write:
            await self._repo_save_terminal(terminal.mark_idle())
        else:
            try:
                await self._process_manager.terminate_terminal(terminal.terminal_id)
            except Exception:
                logger.exception(
                    "Failed to terminate terminal after indeterminate command write: terminal_id=%s command_id=%s",
                    terminal.terminal_id,
                    command.command_id,
                )
            try:
                await self._repo_save_terminal(terminal.mark_error())
            except ValueError:
                await self._repo_save_terminal(terminal.mark_idle())
            await self._release_live_slot(terminal.terminal_id)
            self._runtime_tokens.pop(terminal.terminal_id, None)
        # Order: persist → publish → Event.set → cleanup wait registry.
        await self._publish(
            scope,
            "terminal.session.completed",
            {
                "terminal_session_id": command.command_id,
                "terminal_id": terminal.terminal_id,
                "command_id": command.command_id,
                "tool_call_id": scope.tool_call_id or command.command_id,
                "exit_code": completed.exit_code,
                "duration_ms": completed.duration_ms,
                "stdout_bytes": completed.stdout_bytes,
                "stderr_bytes": completed.stderr_bytes,
                "error_message": completed.error_message,
                "completed_at": completed.completed_at.isoformat() if completed.completed_at else None,
            },
            correlation_id=scope.tool_call_id,
        )
        self._signal_command_finished(command.command_id)
        self._cleanup_command_wait_state(terminal.terminal_id, command.command_id)
        logger.warning(
            "Terminal execute failed: terminal_id=%s command_id=%s error_type=%s error=%s task_id=%s agent_instance_id=%s duration_ms=%s",
            terminal.terminal_id,
            command.command_id,
            error_type or finalizer,
            error_message,
            scope.task_id,
            scope.agent_instance_id,
            int((time.perf_counter() - started_at) * 1000),
        )

    async def _persist_and_publish_output(
        self,
        scope: TerminalRuntimeScope,
        *,
        terminal_id: str,
        command_id: str,
        stream: str,
        text: str,
        coalesced: bool,
        source_chunk_count: int,
    ) -> None:
        async with self._lock_for(f"output:{terminal_id}:{command_id}"):
            chunk_index, byte_offset = await self._append_output_chunk(
                terminal_id=terminal_id,
                command_id=command_id,
                stream=stream,
                text=text,
            )
        await self._publish_output_delta(
            scope,
            terminal_id=terminal_id,
            command_id=command_id,
            stream=stream,
            text=text,
            chunk_index=chunk_index,
            byte_offset=byte_offset,
            coalesced=coalesced,
            source_chunk_count=source_chunk_count,
        )

    async def _append_output_chunk(
        self,
        *,
        terminal_id: str,
        command_id: str,
        stream: str,
        text: str,
    ) -> tuple[int, int]:
        encoded = text.encode("utf-8", errors="replace")
        key = (terminal_id, command_id)
        byte_offset = self._byte_offsets.get(key, 0)
        chunk_index = await monitored_to_thread(
            self._repo.next_output_chunk_index,
            terminal_id,
            command_id,
            operation="terminal.repo.next_output_chunk_index",
            caller="TerminalService._append_output_chunk",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={"terminal_id": terminal_id, "command_id": command_id},
        )
        chunk = TerminalOutputChunk(
            terminal_id=terminal_id,
            command_id=command_id,
            chunk_index=chunk_index,
            byte_offset=byte_offset,
            stream=stream,
            chunk=text,
        )
        await monitored_to_thread(
            self._repo.append_output_chunk,
            chunk,
            operation="terminal.repo.append_output_chunk",
            caller="TerminalService._append_output_chunk",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={
                "terminal_id": terminal_id,
                "command_id": command_id,
                "chunk_index": chunk_index,
                "stream": stream,
            },
        )
        self._byte_offsets[key] = byte_offset + len(encoded)
        stream_key = (terminal_id, command_id, stream)
        self._stream_bytes[stream_key] = self._stream_bytes.get(stream_key, 0) + len(encoded)
        return chunk_index, byte_offset

    async def _publish_output_delta(
        self,
        scope: TerminalRuntimeScope,
        *,
        terminal_id: str,
        command_id: str,
        stream: str,
        text: str,
        chunk_index: int,
        byte_offset: int,
        coalesced: bool,
        source_chunk_count: int,
    ) -> None:
        payload: dict[str, Any] = {
            "terminal_session_id": command_id,
            "terminal_id": terminal_id,
            "command_id": command_id,
            "tool_call_id": scope.tool_call_id or command_id,
            "stream": stream,
            "chunk": text,
            "chunk_index": chunk_index,
            "byte_offset": byte_offset,
            "encoding": "utf-8",
        }
        if coalesced:
            payload["coalesced"] = True
            payload["chunk_count"] = source_chunk_count
        self._output_ring.append(
            terminal_id=terminal_id,
            command_id=command_id,
            chunk_index=chunk_index,
            stream=stream,
            chunk=text,
        )
        # The since contract is persisted-output OR finished; do not wait for a slow event gateway.
        self._signal_command_output(command_id)
        await self._publish(
            scope,
            "terminal.output.delta",
            payload,
            correlation_id=scope.tool_call_id,
        )

    def _resolve_shell(self, shell: str | None) -> str:
        return resolve_supported_shell(shell or self._default_shell)

    async def _wait_for_read(
        self,
        *,
        terminal_id: str,
        command_id: str,
        mode: ReadMode,
        after_chunk_index: int | None,
        wait_timeout_ms: int,
    ) -> None:
        deadline = time.perf_counter() + (wait_timeout_ms / 1000)
        seen_output_version = self._command_output_versions.get(command_id, 0)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            finish_event = self._command_finish_events.get(command_id)
            if finish_event is not None and finish_event.is_set():
                return
            command = await self._repo_get_command(command_id)
            if command is not None and command.status in TERMINAL_COMMAND_FINISHED_STATUSES:
                return
            if mode == "since" and after_chunk_index is not None:
                chunks = await self._read_output_repo_list(
                    terminal_id=terminal_id,
                    command_id=command_id,
                    after_chunk_index=after_chunk_index,
                    limit=1,
                    mode=mode,
                    operation="terminal.read_output.wait_since_probe",
                )
                if chunks:
                    return
                current_version = self._command_output_versions.get(command_id, 0)
                if current_version > seen_output_version:
                    # Output was signaled between snapshot and version check; re-probe DB.
                    seen_output_version = current_version
                    continue
                output_event = self._command_output_events.setdefault(command_id, asyncio.Event())
                finish_event = self._command_finish_events.setdefault(command_id, asyncio.Event())
                # Re-check after grabbing events to close the register race without shared clear().
                if finish_event.is_set() or self._command_output_versions.get(command_id, 0) > seen_output_version:
                    continue
                wait_finish = asyncio.create_task(finish_event.wait())
                wait_output = asyncio.create_task(output_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {wait_finish, wait_output},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (wait_finish, wait_output):
                        if not task.done():
                            task.cancel()
                    for task in (wait_finish, wait_output):
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                if not done:
                    return
                if wait_output in done:
                    seen_output_version = self._command_output_versions.get(command_id, seen_output_version)
                continue
            finish_event = self._command_finish_events.setdefault(command_id, asyncio.Event())
            if finish_event.is_set():
                return
            try:
                await asyncio.wait_for(finish_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            return

    def _register_command_wait_state(self, terminal_id: str, command_id: str) -> None:
        self._command_finish_events[command_id] = asyncio.Event()
        self._command_output_events[command_id] = asyncio.Event()
        self._command_output_versions[command_id] = 0
        self._terminal_wait_commands.setdefault(terminal_id, set()).add(command_id)

    def _cleanup_command_wait_state(self, terminal_id: str, command_id: str) -> None:
        self._command_finish_events.pop(command_id, None)
        self._command_output_events.pop(command_id, None)
        self._command_output_versions.pop(command_id, None)
        command_ids = self._terminal_wait_commands.get(terminal_id)
        if command_ids is not None:
            command_ids.discard(command_id)
            if not command_ids:
                self._terminal_wait_commands.pop(terminal_id, None)

    def _wake_and_cleanup_terminal_wait_states(self, terminal_id: str) -> None:
        command_ids = list(self._terminal_wait_commands.get(terminal_id, ()))
        for command_id in command_ids:
            self._signal_command_finished(command_id)
            self._cleanup_command_wait_state(terminal_id, command_id)
        self._terminal_wait_commands.pop(terminal_id, None)

    def _signal_command_finished(self, command_id: str) -> None:
        event = self._command_finish_events.get(command_id)
        if event is None:
            # Keep a one-shot finished event so late readers can observe completion.
            event = asyncio.Event()
            self._command_finish_events[command_id] = event
        event.set()
        # Replace output event after set so multi-waiters observe generation without shared clear().
        old_output = self._command_output_events.get(command_id)
        if old_output is not None:
            self._command_output_events[command_id] = asyncio.Event()
            old_output.set()

    def _signal_command_output(self, command_id: str) -> None:
        # Do not recreate wait registry after READY/close cleanup.
        if command_id not in self._command_output_versions and command_id not in self._command_finish_events:
            return
        self._command_output_versions[command_id] = self._command_output_versions.get(command_id, 0) + 1
        old_event = self._command_output_events.get(command_id)
        self._command_output_events[command_id] = asyncio.Event()
        if old_event is not None:
            old_event.set()

    async def _repo_get_command(self, command_id: str) -> TerminalCommand | None:
        return await monitored_to_thread(
            self._repo.get_command,
            command_id,
            operation="terminal.repo.get_command",
            caller="TerminalService",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={"command_id": command_id},
        )

    async def _repo_get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        return await monitored_to_thread(
            self._repo.get_terminal,
            terminal_id,
            operation="terminal.repo.get_terminal",
            caller="TerminalService",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={"terminal_id": terminal_id},
        )

    async def _repo_save_command(self, command: TerminalCommand) -> TerminalCommand:
        return await monitored_to_thread(
            self._repo.save_command,
            command,
            operation="terminal.repo.save_command",
            caller="TerminalService",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={"command_id": command.command_id, "terminal_id": command.terminal_id},
        )

    async def _repo_save_terminal(self, terminal: TerminalInstance) -> TerminalInstance:
        return await monitored_to_thread(
            self._repo.save_terminal,
            terminal,
            operation="terminal.repo.save_terminal",
            caller="TerminalService",
            pool=BlockingPool.DB_BLOCKING,
            log=logger,
            diagnostic_context={"terminal_id": terminal.terminal_id, "status": terminal.status},
        )

    @staticmethod
    def _resolve_cwd(cwd: str | None, default_cwd: str) -> str:
        actual = cwd or default_cwd
        path = Path(actual).expanduser()
        if not path.is_absolute():
            path = Path(default_cwd).expanduser() / path
        return str(path)


def _is_definite_unwritten_command_error(exc: BaseException) -> bool:
    """True only when command text is known not to have reached the PTY."""
    return isinstance(exc, BlockingPoolOverloadedError)


def _build_read_result(
    *,
    terminal_id: str,
    command_id: str | None,
    command: TerminalCommand | None,
    status: str,
    chunks: list[TerminalOutputChunk],
    text: str,
    limit_chars: int,
    has_more: bool,
) -> TerminalOutputReadResult:
    truncated = len(text) > limit_chars
    actual_text = text[-limit_chars:] if truncated else text
    start_index = chunks[0].chunk_index if chunks else None
    end_index = chunks[-1].chunk_index if chunks else None
    return TerminalOutputReadResult(
        terminal_id=terminal_id,
        command_id=command_id,
        command_status=command.status if command is not None else None,
        command_exit_code=command.exit_code if command is not None else None,
        command_completed_at=command.completed_at.isoformat() if command is not None and command.completed_at is not None else None,
        command_finished=command.status in TERMINAL_COMMAND_FINISHED_STATUSES if command is not None else None,
        status=status,
        text=actual_text,
        chunks_returned=len(chunks),
        truncated=truncated,
        start_chunk_index=start_index,
        end_chunk_index=end_index,
        next_chunk_index=(end_index + 1) if end_index is not None else None,
        has_more=has_more or truncated,
    )


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _default_shell_name() -> str:
    return platform_supported_shell()


def _control_text(control: str | None) -> str:
    if control == "enter":
        return "\n"
    if control == "ctrl_d":
        return "\x04"
    if control == "escape":
        return "\x1b"
    raise ValueError(f"未知终端控制输入: {control}")



