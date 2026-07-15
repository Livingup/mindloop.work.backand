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
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError
from agent_runtime.terminal.terminal_factory import TerminalFactory
from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult, ToolRuntimeContext


class TerminalCreateToolRequest(BaseToolRequest):
    cwd: str = Field(description="要启动终端的工作目录，必须显式提供。该目录只作为 shell 初始目录；如果你之后在终端内 cd，工具返回的 cwd 不会更新，你需要自己根据对话上下文记住当前目录。")
    shell: str | None = Field(default=None, description="可选 shell 名称，例如 powershell、pwsh、cmd、bash；不传时使用默认 shell。该值只在创建终端时生效。")


class TerminalExecuteCommandToolRequest(BaseToolRequest):
    command_line: str = Field(description="要在指定终端中输入并回车执行的命令文本。工具只负责发送这段文本，不会替你判断命令是否结束或是否成功。")
    terminal_id: str = Field(description="目标终端 ID，必须来自 terminal_create 或 terminal_list。该字段必填；不要自己编造 terminal_id；如果还没有终端，先调用 terminal_create。")


class TerminalReadOutputToolRequest(BaseToolRequest):
    terminal_id: str = Field(description="要观察的终端 ID，必须来自 terminal_create 或 terminal_list。")
    command_id: str = Field(description="要观察的命令 ID，必须来自 terminal_execute_command 返回的 command_id；用于关联读取该命令产生的终端输出，不表示后端会判断 shell 命令是否完成。")
    mode: Literal["tail", "page", "since", "search"] = Field(default="tail", description="读取方式；可选值仅限 tail、page、since、search。tail 读取终端最新可见输出，page 分页读取输出，since 读取指定输出块之后的新内容，search 搜索输出文本。")
    tail_lines: int = Field(default=80, ge=1, le=1000, description="tail 模式最多返回最新多少行；取值范围为 1 到 1000。")
    offset: int = Field(default=0, ge=0, description="page 模式从第几个输出块开始读取；取值必须大于等于 0；该值不是文本行号。")
    limit_lines: int = Field(default=200, ge=1, le=2000, description="最多读取多少个输出块；取值范围为 1 到 2000；该值不是最终返回文本的行数。")
    limit_chars: int = Field(default=12000, ge=1000, le=50000, description="最多返回多少个字符，避免一次读取过多内容；取值范围为 1000 到 50000。")
    after_chunk_index: int | None = Field(default=None, ge=0, description="since 模式只读取该输出块序号之后的新内容；如提供则取值必须大于等于 0。可结合上次读取结果中的 end_chunk_index 或 next_chunk_index 继续增量观察。")
    wait_timeout_ms: int = Field(default=0, ge=0, le=600000, description="等待 wait_timeout_ms 时间后读取终端输出，单位毫秒；应根据目标命令预估合理完成时间设置，不要默认使用最大值；取值范围为 0 到 600000（最多 10 分钟）；0 表示立即读取不等待。")
    query: str | None = Field(default=None, description="search 模式要搜索的关键词；使用 search 模式时必须提供。")
    case_sensitive: bool = Field(default=False, description="search 模式是否区分大小写；true 表示区分大小写，false 表示不区分大小写。")


class TerminalWriteInputToolRequest(BaseToolRequest):
    terminal_id: str = Field(description="要写入交互输入的终端 ID，必须来自 terminal_create 或 terminal_list。")
    text: str | None = Field(default=None, description="要输入到终端的普通文本；发送控制键时可以为空。")
    append_newline: bool = Field(default=False, description="是否在 text 末尾追加换行；true 表示追加换行，false 表示不追加。")
    control: Literal["enter", "ctrl_c", "ctrl_d", "escape"] | None = Field(
        default=None,
        description="要发送的控制键；可选值仅限 enter、ctrl_c、ctrl_d、escape。ctrl_c 表示中断当前前台进程；如果终端当前没有前台进程，ctrl_c 会安全返回成功但不会产生输出。",
    )


@tool(
    name="terminal_create",
    description="打开一个可交互的真实终端，返回 terminal_id，供后续发送命令、读取输出或写入交互输入。创建的终端是持久终端，尽量复用可用的终端，不要随意创建新终端，导致资源浪费和内存泄露。你必须显式提供初始 cwd；cwd 只表示 shell 启动目录，之后如果在终端内切换目录，需要你自己记住。一次对话结束后，这些终端会自动清理。",
    group="terminal",
    category="business",
)
async def terminal_create(request: TerminalCreateToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    try:
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
    except TerminalCapacityExceededError as exc:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code=exc.error_code,
            error_message=str(exc),
            output=exc.to_metadata(),
            metadata=exc.to_metadata(),
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
    description="在已有终端中输入一条命令并回车执行，返回 command_id。必须显式传入 terminal_id。工具只表示命令已被发送到终端，不表示命令已经结束或成功；你需要用 terminal_read_output 观察输出，并自行判断是否完成、是否报错、是否需要继续等待或输入。",
    group="terminal",
    category="business",
)
async def terminal_execute_command(request: TerminalExecuteCommandToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    gateway = TerminalFactory.create()
    try:
        result = await gateway.execute_command(
            ExecuteTerminalCommandRequest(
                command_line=request.command_line,
                terminal_id=request.terminal_id,
                task_id=ctx.task_id,
                agent_instance_id=ctx.agent_instance_id,
                communication_stream_id=ctx.communication_stream_id,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
            )
        )
    except TerminalCapacityExceededError as exc:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code=exc.error_code,
            error_message=str(exc),
            output=exc.to_metadata(),
            metadata=exc.to_metadata(),
        )
    return BaseToolResult(
        success=result.accepted,
        execution_status="success" if result.accepted else "failed",
        output={
            "accepted": result.accepted,
            "terminal_id": result.terminal_id,
            "command_id": result.command_id,
        },
    )


@tool(
    name="terminal_read_output",
    description="读取终端输出，用于观察命令进展、错误信息、交互提示或最终结果。必须传入 terminal_id 和 command_id。支持 tail、page、since、search 四种读取方式；默认读取最新输出并限制返回字符数。返回结果包含输出文本、分页位置、是否还有更多内容，以及可用时的命令记录状态字段；这些状态字段不代表后端判断当前 shell 命令已经完成。wait_timeout_ms 表示读取前固定等待的时间（毫秒），到达等待时间后读取当前可用输出；短命令使用较小等待时间，构建、测试等长命令可使用更长等待时间，不要默认填最大值。是否需要继续等待或写入输入，应由你结合终端文本自行判断。",
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
    description="向已有终端写入交互输入，例如回答命令提示、发送回车、发送 Ctrl+C 中断前台进程。你应先用 terminal_read_output 观察输出，再决定是否需要输入。普通文本、enter、ctrl_d、escape 适用于仍在运行并等待交互的命令；ctrl_c 用于尝试中断当前前台进程。",
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
    description="列出你在本轮对话中已经打开且仍可使用的终端，用于找回 terminal_id、查看 shell 和终端状态。返回的 cwd 只表示终端创建时的初始目录，不代表 shell 当前目录；如果你在终端内执行过 cd，需要你自己根据上下文判断当前目录。",
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
