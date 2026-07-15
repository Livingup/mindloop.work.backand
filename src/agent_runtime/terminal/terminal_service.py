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
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.terminal.terminal import (
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
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError
from agent_runtime.terminal.terminal_output import TerminalOutputChunk
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
        actual_agent_instance_id = agent_instance_id

        if self._agent_runtime_gateway is not None and task_id:
            task = self._agent_runtime_gateway.get_task(task_id)
            actual_scene_id = actual_scene_id or task.scene_id
            if actual_agent_instance_id is None:
                actual_agent_instance_id = task.root_agent_instance_id
        if self._agent_runtime_gateway is not None and actual_agent_instance_id:
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
        max_output_chars_per_read: int = 50000,
        max_live_terminals: int = 200,
        max_live_terminals_per_agent: int = 16,
        output_coalesce_interval_ms: int = 33,
        output_coalesce_max_bytes: int = 8192,
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
        self._output_coalesce_max_bytes = max(1, output_coalesce_max_bytes)
        self._locks: dict[str, asyncio.Lock] = {}
        self._byte_offsets: dict[tuple[str, str], int] = {}
        self._stream_bytes: dict[tuple[str, str, str], int] = {}
        self._runtime_tokens: dict[str, str] = {}
        # terminal_id -> agent_instance_id for currently mounted live runtimes
        self._live_slots: dict[str, str | None] = {}
        self._capacity_lock = asyncio.Lock()
        self._pending_outputs: dict[tuple[str, str, str], _PendingTerminalOutput] = {}

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
        return self._repo.list_terminals_by_owner(scope.owner_type, scope.owner_id)

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
            actual_cwd = self._resolve_cwd(terminal.cwd, scope.default_cwd)
            actual_shell = self._resolve_shell(terminal.shell)
            await self._ensure_terminal_runtime(scope, terminal, cwd=actual_cwd, shell=actual_shell)
            terminal = self._repo.get_terminal(terminal.terminal_id) or terminal
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
            terminal = terminal.mark_idle()
            self._repo.save_terminal(terminal)
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
        except Exception as exc:
            failed = command.fail(str(exc))
            self._repo.save_command(failed)
            self._repo.save_terminal(terminal.mark_idle())
            await self._publish(
                scope,
                "terminal.session.completed",
                {
                    "terminal_session_id": command.command_id,
                    "terminal_id": terminal.terminal_id,
                    "command_id": command.command_id,
                    "tool_call_id": scope.tool_call_id or command.command_id,
                    "exit_code": failed.exit_code,
                    "duration_ms": failed.duration_ms,
                    "stdout_bytes": failed.stdout_bytes,
                    "stderr_bytes": failed.stderr_bytes,
                    "error_message": failed.error_message,
                    "completed_at": failed.completed_at.isoformat() if failed.completed_at else None,
                },
                correlation_id=scope.tool_call_id,
            )
            logger.warning(
                "Terminal execute failed: terminal_id=%s command_id=%s error_type=%s error=%s task_id=%s agent_instance_id=%s duration_ms=%s",
                terminal.terminal_id,
                command.command_id,
                exc.__class__.__name__,
                str(exc),
                scope.task_id,
                scope.agent_instance_id,
                int((time.perf_counter() - started_at) * 1000),
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
        if effective_wait_timeout_ms > 0:
            await asyncio.sleep(effective_wait_timeout_ms / 1000)
            terminal = self._require_terminal(scope, terminal_id)
            read_command = self._get_read_command(terminal_id, command_id)
        result: TerminalOutputReadResult
        if mode == "search":
            if not query:
                raise ValueError("search 模式必须提供 query。")
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
        self._runtime_tokens.pop(terminal_id, None)
        await self._process_manager.terminate_terminal(terminal_id)
        await self._release_live_slot(terminal_id)
        detached = terminal.mark_idle()
        self._repo.save_terminal(detached)
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
            terminal = self._repo.get_terminal(terminal_id)
            if terminal is None or terminal.owner_type != scope.owner_type or terminal.owner_id != scope.owner_id:
                return await self.create_terminal(scope, cwd=cwd, shell=shell, terminal_id=terminal_id)
            if terminal.status == TERMINAL_STATUS_DELETED:
                raise KeyError(f"终端已删除，不能复用: {terminal_id}")
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
        if terminal is None or terminal.owner_type != scope.owner_type or terminal.owner_id != scope.owner_id:
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
        async with self._capacity_lock:
            already_live = terminal.terminal_id in self._live_slots
            if not already_live:
                self._assert_can_admit_live(scope.agent_instance_id)
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
                )
            except Exception:
                # start failed before we own a live slot for a new terminal.
                raise
            if not already_live:
                self._live_slots[terminal.terminal_id] = scope.agent_instance_id
            elif started:
                # Replaced a stale runtime; keep the existing live slot ownership.
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
            should_flush_now = pending.pending_bytes >= self._output_coalesce_max_bytes
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
    ) -> None:
        await self._flush_command_outputs(terminal_id, command_id)
        command = self._repo.get_command(command_id)
        terminal = self._repo.get_terminal(terminal_id)
        if command is None or terminal is None:
            return
        stdout_bytes = self._stream_bytes.get((terminal_id, command_id, "stdout"), 0)
        stdout_bytes += self._stream_bytes.get((terminal_id, command_id, "pty"), 0)
        stderr_bytes = self._stream_bytes.get((terminal_id, command_id, "stderr"), 0)
        if error_message:
            completed = command.fail(
                error_message,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        else:
            completed = command.complete(
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        self._repo.save_command(completed)
        if terminal.current_command_id == command_id and terminal.status != TERMINAL_STATUS_DELETED:
            try:
                self._repo.save_terminal(terminal.mark_idle())
            except ValueError:
                self._repo.save_terminal(terminal.mark_error())
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

    async def _handle_terminal_exit(
        self,
        scope: TerminalRuntimeScope,
        terminal_id: str,
        runtime_token: str | None,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        terminal = self._repo.get_terminal(terminal_id)
        if terminal is None or terminal.status == TERMINAL_STATUS_DELETED:
            await self._release_live_slot(terminal_id)
            return
        if runtime_token is not None and self._runtime_tokens.get(terminal_id) != runtime_token:
            return
        await self._flush_terminal_outputs(terminal_id)
        updated = terminal.mark_error() if error_message else terminal.mark_idle()
        self._runtime_tokens.pop(terminal_id, None)
        await self._release_live_slot(terminal_id)
        self._repo.save_terminal(updated)
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
            return

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
            return

    async def _flush_terminal_outputs(self, terminal_id: str) -> None:
        keys = [key for key in self._pending_outputs if key[0] == terminal_id]
        for key in keys:
            await self._flush_pending_output(key)

    async def _flush_command_outputs(self, terminal_id: str, command_id: str) -> None:
        keys = [key for key in self._pending_outputs if key[0] == terminal_id and key[1] == command_id]
        for key in keys:
            await self._flush_pending_output(key)

    async def _flush_pending_output(self, pending_key: tuple[str, str, str]) -> None:
        async with self._lock_for(f"output:{pending_key[0]}:{pending_key[1]}"):
            pending = self._pending_outputs.pop(pending_key, None)
            if pending is None or not pending.parts:
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
            chunk_index, byte_offset = self._append_output_chunk_locked(
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
            coalesced=source_chunk_count > 1,
            source_chunk_count=source_chunk_count,
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
            chunk_index, byte_offset = self._append_output_chunk_locked(
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

    def _append_output_chunk_locked(
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
        chunk_index = self._repo.next_output_chunk_index(terminal_id, command_id)
        chunk = TerminalOutputChunk(
            terminal_id=terminal_id,
            command_id=command_id,
            chunk_index=chunk_index,
            byte_offset=byte_offset,
            stream=stream,
            chunk=text,
        )
        self._repo.append_output_chunk(chunk)
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
        await self._publish(
            scope,
            "terminal.output.delta",
            payload,
            correlation_id=scope.tool_call_id,
        )

    def _resolve_shell(self, shell: str | None) -> str:
        actual = shell or self._default_shell
        if actual == "default":
            return _default_shell_name()
        return actual

    @staticmethod
    def _resolve_cwd(cwd: str | None, default_cwd: str) -> str:
        actual = cwd or default_cwd
        path = Path(actual).expanduser()
        if not path.is_absolute():
            path = Path(default_cwd).expanduser() / path
        return str(path)


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
    if sys.platform.startswith("win"):
        return "powershell"
    return "bash"


def _control_text(control: str | None) -> str:
    if control == "enter":
        return "\n"
    if control == "ctrl_d":
        return "\x04"
    if control == "escape":
        return "\x1b"
    raise ValueError(f"未知终端控制输入: {control}")
