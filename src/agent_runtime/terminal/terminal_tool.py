from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import Field

from agent_runtime.terminal.north_gateway.terminal_contracts import (
    CreateTerminalRequest,
    ExecuteTerminalCommandRequest,
    ListTerminalsRequest,
    ReadTerminalOutputRequest,
    WriteTerminalInputRequest,
)
from agent_runtime.terminal.terminal_factory import TerminalFactory
from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult, ToolRuntimeContext


class TerminalCreateToolRequest(BaseToolRequest):
    cwd: str | None = Field(default=None, description="可选终端工作目录；不传时使用当前任务默认工作目录。")
    shell: str | None = Field(default=None, description="可选 shell；不传时使用服务默认 shell。")


class TerminalExecuteCommandToolRequest(BaseToolRequest):
    command_line: str = Field(description="要执行的命令行。")
    terminal_id: str | None = Field(default=None, description="可选终端 ID；不传时自动选择空闲终端，没有则创建新终端。")
    cwd: str | None = Field(default=None, description="可选本次命令工作目录；不传时使用终端 cwd 或默认工作目录。")
    shell: str | None = Field(default=None, description="可选本次命令 shell；不传时使用终端 shell 或服务默认 shell。")
    timeout_ms: int | None = Field(default=None, ge=1, description="可选命令超时时间，单位毫秒。")


class TerminalReadOutputToolRequest(BaseToolRequest):
    terminal_id: str = Field(description="要读取输出的终端 ID。")
    command_id: str | None = Field(default=None, description="可选命令 ID；不传时读取终端全部输出。")
    mode: Literal["tail", "page", "since", "search"] = Field(default="tail", description="读取模式：tail 最新输出、page 分页、since 游标后增量、search 搜索。")
    tail_lines: int = Field(default=80, ge=1, le=1000, description="tail 模式返回最新多少行。")
    offset: int = Field(default=0, ge=0, description="page 模式输出 chunk 偏移。")
    limit_lines: int = Field(default=200, ge=1, le=2000, description="最多读取的输出 chunk 数。")
    limit_chars: int = Field(default=12000, ge=1000, le=50000, description="最多返回字符数，避免撑爆上下文。")
    after_chunk_index: int | None = Field(default=None, ge=0, description="since 模式读取该 chunk_index 之后的输出。")
    wait_timeout_ms: int = Field(default=0, ge=0, le=60000, description="没有新输出时最多等待多久，0 表示不等待。")
    query: str | None = Field(default=None, description="search 模式搜索关键词。")
    case_sensitive: bool = Field(default=False, description="search 模式是否大小写敏感。")


class TerminalWriteInputToolRequest(BaseToolRequest):
    terminal_id: str = Field(description="要写入输入的终端 ID。")
    text: str | None = Field(default=None, description="要输入的文本。")
    append_newline: bool = Field(default=False, description="是否在文本末尾追加换行。")
    control: Literal["enter", "ctrl_c", "ctrl_d", "escape"] | None = Field(default=None, description="可选控制输入；设置后 text 可为空。")


@tool(
    name="terminal_create",
    description="创建一个当前任务/场景归属的终端。可以指定 cwd 和 shell；不指定 cwd 时使用默认工作目录。",
    group="terminal",
    category="business",
)
async def terminal_create(request: TerminalCreateToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    info = await gateway.create_terminal(
        CreateTerminalRequest(
            cwd=request.cwd,
            shell=request.shell,
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    return BaseToolResult(
        success=True,
        execution_status="success",
        output={
            "terminal_id": info.terminal_id,
            "cwd": info.cwd,
            "shell": info.shell,
            "status": info.status,
        },
    )


@tool(
    name="terminal_execute_command",
    description="在当前任务/场景归属终端中启动命令。可指定 terminal_id/cwd/shell；不指定 terminal_id 时自动选择空闲终端或创建新终端。工具成功表示命令已开始异步执行，返回实际 cwd、terminal_id 和 command_id。",
    group="terminal",
    category="business",
)
async def terminal_execute_command(request: TerminalExecuteCommandToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    result = await gateway.execute_command(
        ExecuteTerminalCommandRequest(
            command_line=request.command_line,
            terminal_id=request.terminal_id,
            cwd=request.cwd,
            shell=request.shell,
            timeout_ms=request.timeout_ms,
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    return BaseToolResult(
        success=result.accepted,
        execution_status="success" if result.accepted else "failed",
        output={
            "accepted": result.accepted,
            "terminal_id": result.terminal_id,
            "command_id": result.command_id,
            "status": result.status,
            "actual_cwd": result.actual_cwd,
            "shell": result.shell,
        },
    )


@tool(
    name="terminal_read_output",
    description="读取当前任务/场景归属终端输出。支持 tail/page/since/search，默认只读最新输出并限制返回字符数，避免大输出撑爆上下文。",
    group="terminal",
    category="business",
)
async def terminal_read_output(request: TerminalReadOutputToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    result = await gateway.read_output(
        ReadTerminalOutputRequest(
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
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    return BaseToolResult(success=True, execution_status="success", output=asdict(result))


@tool(
    name="terminal_write_input",
    description="向当前任务/场景归属终端写入输入，用于交互式命令。可写文本、追加换行，或发送 enter/ctrl_c/ctrl_d/escape 控制输入。",
    group="terminal",
    category="business",
)
async def terminal_write_input(request: TerminalWriteInputToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    result = await gateway.write_input(
        WriteTerminalInputRequest(
            terminal_id=request.terminal_id,
            text=request.text,
            append_newline=request.append_newline,
            control=request.control,
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    return BaseToolResult(
        success=result.accepted,
        execution_status="success" if result.accepted else "failed",
        output={
            "accepted": result.accepted,
            "terminal_id": result.terminal_id,
            "bytes_written": result.bytes_written,
        },
    )


@tool(
    name="terminal_list",
    description="列出当前任务/场景归属的终端及其状态。",
    group="terminal",
    category="business",
)
async def terminal_list(ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    terminals = gateway.list_terminals(
        ListTerminalsRequest(
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    return BaseToolResult(
        success=True,
        execution_status="success",
        output={
            "terminals": [
                {
                    "terminal_id": terminal.terminal_id,
                    "cwd": terminal.cwd,
                    "shell": terminal.shell,
                    "status": terminal.status,
                    "current_command_id": terminal.current_command_id,
                    "created_at": terminal.created_at.isoformat(),
                    "last_active_at": terminal.last_active_at.isoformat(),
                }
                for terminal in terminals
            ]
        },
    )
