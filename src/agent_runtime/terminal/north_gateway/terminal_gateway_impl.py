from __future__ import annotations

from typing import Any

from agent_runtime.terminal.terminal import TerminalInstance
from agent_runtime.terminal.terminal_command import TerminalCommand
from agent_runtime.terminal.terminal_service import TerminalOutputReadResult, TerminalRuntimeScope, TerminalService
from agent_runtime.terminal.north_gateway.terminal_contracts import (
    CloseTerminalRequest,
    CreateTerminalRequest,
    ExecuteTerminalCommandRequest,
    ExecuteTerminalCommandResult,
    ListTerminalsRequest,
    ReadTerminalOutputRequest,
    ReadTerminalOutputResult,
    TerminalCommandInfo,
    TerminalInfo,
    WriteTerminalInputRequest,
    WriteTerminalInputResult,
)


class TerminalGatewayImpl:
    def __init__(self, service: TerminalService) -> None:
        self._service = service

    async def create_terminal(self, request: CreateTerminalRequest) -> TerminalInfo:
        scope = self._scope_from_request(request)
        terminal = await self._service.create_terminal(scope, cwd=request.cwd, shell=request.shell)
        return _terminal_to_info(terminal)

    async def execute_command(self, request: ExecuteTerminalCommandRequest) -> ExecuteTerminalCommandResult:
        scope = self._scope_from_request(request)
        accepted = await self._service.execute_command(
            scope,
            command_line=request.command_line,
            terminal_id=request.terminal_id,
            cwd=request.cwd,
            shell=request.shell,
            timeout_ms=request.timeout_ms,
        )
        return ExecuteTerminalCommandResult(
            accepted=accepted.accepted,
            terminal_id=accepted.terminal.terminal_id,
            command_id=accepted.command.command_id,
            status=accepted.command.status,
            actual_cwd=accepted.command.actual_cwd,
            shell=accepted.command.shell,
        )

    async def read_output(self, request: ReadTerminalOutputRequest) -> ReadTerminalOutputResult:
        scope = self._scope_from_request(request)
        result = await self._service.read_output(
            scope,
            terminal_id=request.terminal_id,
            command_id=request.command_id,
            mode=request.mode,
            tail_lines=request.tail_lines,
            offset=request.offset,
            limit_lines=request.limit_lines,
            limit_chars=request.limit_chars,
            after_chunk_index=request.after_chunk_index,
            wait_timeout_ms=request.wait_timeout_ms,
            query=request.query,
            case_sensitive=request.case_sensitive,
        )
        return _read_result_to_dto(result)

    async def write_input(self, request: WriteTerminalInputRequest) -> WriteTerminalInputResult:
        scope = self._scope_from_request(request)
        bytes_written = await self._service.write_input(
            scope,
            terminal_id=request.terminal_id,
            text=request.text,
            append_newline=request.append_newline,
            control=request.control,
        )
        return WriteTerminalInputResult(accepted=True, terminal_id=request.terminal_id, bytes_written=bytes_written)

    def list_terminals(self, request: ListTerminalsRequest) -> list[TerminalInfo]:
        scope = self._scope_from_request(request)
        return [
            _terminal_to_info(
                terminal,
                latest_command=self._service.get_latest_command(terminal.terminal_id),
            )
            for terminal in self._service.list_terminals(scope)
        ]

    async def close_terminal(self, request: CloseTerminalRequest) -> TerminalInfo:
        scope = self._scope_from_request(request)
        terminal = await self._service.close_terminal(scope, request.terminal_id)
        return _terminal_to_info(terminal)

    async def delete_terminal(self, request: CloseTerminalRequest) -> TerminalInfo:
        scope = self._scope_from_request(request)
        terminal = await self._service.delete_terminal(scope, request.terminal_id)
        return _terminal_to_info(terminal)

    async def delete_task_terminals(self, task_id: str) -> list[TerminalInfo]:
        terminals = await self._service.delete_task_terminals(task_id)
        return [_terminal_to_info(terminal) for terminal in terminals]

    async def shutdown(self) -> None:
        await self._service.shutdown()

    def _scope_from_request(self, request: Any) -> TerminalRuntimeScope:
        return self._service.resolve_scope(
            scene_id=getattr(request, "scene_id", None),
            task_id=getattr(request, "task_id", None),
            agent_instance_id=getattr(request, "agent_instance_id", None),
            communication_stream_id=getattr(request, "communication_stream_id", None),
            request_id=getattr(request, "request_id", None),
            trace_id=getattr(request, "trace_id", None),
            workspace_path=getattr(request, "workspace_path", None),
        )


def _terminal_to_info(terminal: TerminalInstance, *, latest_command: TerminalCommand | None = None) -> TerminalInfo:
    return TerminalInfo(
        terminal_id=terminal.terminal_id,
        cwd=terminal.cwd,
        shell=terminal.shell,
        status=terminal.status,
        current_command_id=terminal.current_command_id,
        created_at=terminal.created_at,
        updated_at=terminal.updated_at,
        last_active_at=terminal.last_active_at,
        metadata=dict(terminal.metadata or {}),
        latest_command=_command_to_info(latest_command) if latest_command is not None else None,
    )


def _command_to_info(command: TerminalCommand) -> TerminalCommandInfo:
    return TerminalCommandInfo(
        command_id=command.command_id,
        terminal_id=command.terminal_id,
        command_line=command.command_line,
        actual_cwd=command.actual_cwd,
        shell=command.shell,
        status=command.status,
        requested_cwd=command.requested_cwd,
        tool_call_id=command.tool_call_id,
        started_at=command.started_at,
        completed_at=command.completed_at,
        exit_code=command.exit_code,
        duration_ms=command.duration_ms,
        stdout_bytes=command.stdout_bytes,
        stderr_bytes=command.stderr_bytes,
        error_message=command.error_message,
    )


def _read_result_to_dto(result: TerminalOutputReadResult) -> ReadTerminalOutputResult:
    return ReadTerminalOutputResult(
        terminal_id=result.terminal_id,
        command_id=result.command_id,
        command_status=result.command_status,
        command_exit_code=result.command_exit_code,
        command_completed_at=result.command_completed_at,
        command_finished=result.command_finished,
        status=result.status,
        text=result.text,
        chunks_returned=result.chunks_returned,
        truncated=result.truncated,
        start_chunk_index=result.start_chunk_index,
        end_chunk_index=result.end_chunk_index,
        next_chunk_index=result.next_chunk_index,
        has_more=result.has_more,
    )
