from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from agent_runtime.terminal.north_gateway.terminal_contracts import (
    CloseTerminalRequest,
    CreateTerminalRequest,
    ExecuteTerminalCommandRequest,
    ExecuteTerminalCommandResult,
    ListTerminalsRequest,
    ReadTerminalOutputRequest,
    ReadTerminalOutputResult,
    TerminalGateway,
    TerminalInfo,
    WriteTerminalInputRequest,
    WriteTerminalInputResult,
)
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError

ReadMode = Literal["tail", "page", "since", "search"]


class TerminalCommandResponse(BaseModel):
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


class TerminalResponse(BaseModel):
    terminal_id: str
    cwd: str | None = None
    shell: str
    status: str
    current_command_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime
    metadata: dict[str, object] = {}
    latest_command: TerminalCommandResponse | None = None


class CreateTerminalBody(BaseModel):
    workspace_path: str = Field(description="当前 scene 所属 workspace 绝对路径。")
    cwd: str | None = Field(default=None, description="可选终端工作目录；为空时使用默认工作目录。")
    shell: str | None = Field(default=None, description="可选 shell；为空时使用服务默认 shell。")


class ExecuteTerminalCommandBody(BaseModel):
    workspace_path: str = Field(description="当前 scene 所属 workspace 绝对路径。")
    command_line: str = Field(description="要执行的命令行。")
    cwd: str | None = Field(default=None, description="可选初始工作目录；仅在自动创建新终端时生效，不能改变已有终端 cwd。")
    shell: str | None = Field(default=None, description="可选初始 shell；仅在自动创建新终端时生效，不能改变已有终端 shell。")
    timeout_ms: int | None = Field(default=None, ge=1, description="可选命令超时时间，单位毫秒；如提供则取值必须大于等于 1。")


class ExecuteTerminalCommandResponse(BaseModel):
    accepted: bool
    terminal_id: str
    command_id: str
    status: str
    actual_cwd: str
    shell: str


class ReadTerminalOutputResponse(BaseModel):
    terminal_id: str
    command_id: str | None
    command_status: str | None = None
    command_exit_code: int | None = None
    command_completed_at: str | None = None
    command_finished: bool | None = None
    status: str
    text: str
    chunks_returned: int
    truncated: bool
    start_chunk_index: int | None
    end_chunk_index: int | None
    next_chunk_index: int | None
    has_more: bool


class WriteTerminalInputBody(BaseModel):
    workspace_path: str = Field(description="当前 scene 所属 workspace 绝对路径。")
    text: str | None = Field(default=None, description="要输入的文本。")
    append_newline: bool = Field(default=False, description="是否在文本末尾追加换行。")
    control: Literal["enter", "ctrl_c", "ctrl_d", "escape"] | None = Field(default=None, description="可选控制输入；可选值仅限 enter、ctrl_c、ctrl_d、escape。")


class WriteTerminalInputResponse(BaseModel):
    accepted: bool
    terminal_id: str
    bytes_written: int


def create_terminal_router(gateway: TerminalGateway) -> APIRouter:
    router = APIRouter()

    @router.get("/{scene_id}/terminals", response_model=list[TerminalResponse])
    def list_terminals(
        scene_id: str,
        workspace_path: str = Query(description="当前 scene 所属 workspace 绝对路径。"),
    ) -> list[TerminalResponse]:
        try:
            terminals = gateway.list_terminals(
                ListTerminalsRequest(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return [_terminal_to_response(terminal) for terminal in terminals]

    @router.post("/{scene_id}/terminals", response_model=TerminalResponse, status_code=201)
    async def create_terminal(scene_id: str, body: CreateTerminalBody) -> TerminalResponse:
        try:
            terminal = await gateway.create_terminal(
                CreateTerminalRequest(
                    scene_id=scene_id,
                    workspace_path=body.workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    cwd=body.cwd,
                    shell=body.shell,
                )
            )
        except TerminalCapacityExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error_code": exc.error_code, "message": str(exc), **exc.to_metadata()},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _terminal_to_response(terminal)

    @router.post("/{scene_id}/terminals/{terminal_id}/commands", response_model=ExecuteTerminalCommandResponse)
    async def execute_command(scene_id: str, terminal_id: str, body: ExecuteTerminalCommandBody) -> ExecuteTerminalCommandResponse:
        try:
            result = await gateway.execute_command(
                ExecuteTerminalCommandRequest(
                    scene_id=scene_id,
                    workspace_path=body.workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    terminal_id=terminal_id,
                    command_line=body.command_line,
                    cwd=body.cwd,
                    shell=body.shell,
                    timeout_ms=body.timeout_ms,
                )
            )
        except TerminalCapacityExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error_code": exc.error_code, "message": str(exc), **exc.to_metadata()},
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _execute_result_to_response(result)

    @router.get("/{scene_id}/terminals/{terminal_id}/output", response_model=ReadTerminalOutputResponse)
    async def read_output(
        scene_id: str,
        terminal_id: str,
        workspace_path: str = Query(description="当前 scene 所属 workspace 绝对路径。"),
        command_id: str | None = None,
        mode: ReadMode = "tail",
        tail_lines: int = Query(default=80, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        limit_lines: int = Query(default=200, ge=1, le=2000),
        limit_chars: int = Query(default=12000, ge=1000, le=50000),
        after_chunk_index: int | None = Query(default=None, ge=0),
        wait_timeout_ms: int = Query(default=0, ge=0, le=600000),
        query: str | None = None,
        case_sensitive: bool = False,
    ) -> ReadTerminalOutputResponse:
        try:
            result = await gateway.read_output(
                ReadTerminalOutputRequest(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    terminal_id=terminal_id,
                    command_id=command_id,
                    mode=mode,
                    tail_lines=tail_lines,
                    offset=offset,
                    limit_lines=limit_lines,
                    limit_chars=limit_chars,
                    after_chunk_index=after_chunk_index,
                    wait_timeout_ms=wait_timeout_ms,
                    query=query,
                    case_sensitive=case_sensitive,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _read_result_to_response(result)

    @router.post("/{scene_id}/terminals/{terminal_id}/input", response_model=WriteTerminalInputResponse)
    async def write_input(scene_id: str, terminal_id: str, body: WriteTerminalInputBody) -> WriteTerminalInputResponse:
        try:
            result = await gateway.write_input(
                WriteTerminalInputRequest(
                    scene_id=scene_id,
                    workspace_path=body.workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    terminal_id=terminal_id,
                    text=body.text,
                    append_newline=body.append_newline,
                    control=body.control,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return WriteTerminalInputResponse(
            accepted=result.accepted,
            terminal_id=result.terminal_id,
            bytes_written=result.bytes_written,
        )

    @router.post("/{scene_id}/terminals/{terminal_id}/close", response_model=TerminalResponse)
    async def close_terminal(
        scene_id: str,
        terminal_id: str,
        workspace_path: str = Query(description="当前 scene 所属 workspace 绝对路径。"),
    ) -> TerminalResponse:
        try:
            terminal = await gateway.close_terminal(
                CloseTerminalRequest(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    terminal_id=terminal_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _terminal_to_response(terminal)

    @router.delete("/{scene_id}/terminals/{terminal_id}", response_model=TerminalResponse)
    async def delete_terminal(
        scene_id: str,
        terminal_id: str,
        workspace_path: str = Query(description="当前 scene 所属 workspace 绝对路径。"),
    ) -> TerminalResponse:
        try:
            terminal = await gateway.delete_terminal(
                CloseTerminalRequest(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    communication_stream_id=_scene_stream_id(scene_id),
                    terminal_id=terminal_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _terminal_to_response(terminal)

    return router

def _terminal_to_response(info: TerminalInfo) -> TerminalResponse:
    return TerminalResponse(
        terminal_id=info.terminal_id,
        cwd=info.cwd,
        shell=info.shell,
        status=info.status,
        current_command_id=info.current_command_id,
        created_at=info.created_at,
        updated_at=info.updated_at,
        last_active_at=info.last_active_at,
        metadata=dict(info.metadata),
        latest_command=_command_to_response(info.latest_command) if info.latest_command is not None else None,
    )


def _command_to_response(info) -> TerminalCommandResponse:
    return TerminalCommandResponse(
        command_id=info.command_id,
        terminal_id=info.terminal_id,
        command_line=info.command_line,
        actual_cwd=info.actual_cwd,
        shell=info.shell,
        status=info.status,
        requested_cwd=info.requested_cwd,
        tool_call_id=info.tool_call_id,
        started_at=info.started_at,
        completed_at=info.completed_at,
        exit_code=info.exit_code,
        duration_ms=info.duration_ms,
        stdout_bytes=info.stdout_bytes,
        stderr_bytes=info.stderr_bytes,
        error_message=info.error_message,
    )


def _execute_result_to_response(result: ExecuteTerminalCommandResult) -> ExecuteTerminalCommandResponse:
    return ExecuteTerminalCommandResponse(
        accepted=result.accepted,
        terminal_id=result.terminal_id,
        command_id=result.command_id,
        status=result.status,
        actual_cwd=result.actual_cwd,
        shell=result.shell,
    )


def _read_result_to_response(result: ReadTerminalOutputResult) -> ReadTerminalOutputResponse:
    return ReadTerminalOutputResponse(
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


def _scene_stream_id(scene_id: str) -> str:
    return f"stream-scene-{scene_id}"
