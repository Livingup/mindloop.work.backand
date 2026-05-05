# -*- coding: utf-8 -*-
"""Agent 执行循环引擎。"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_trace_recorder import AgentTraceIdentity, AgentTraceRecorder
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    CreateTaskWithRootAgentInstanceRequest,
)
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionNorthGateway,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import (
    PartMeta,
    TextPart,
    TextPartMeta,
    ToolUsePart,
    UniMessage,
    UniPart,
    ROLE_TOOL,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import LlmRequestService
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpClientGateway
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetNorthGateway,
)
from agent_runtime.skill.north_gateway.skill_contracts import SkillGateway
from agent_runtime.tool.north_gateway.local_tool_contracts import LocalToolGateway
from agent_runtime.tool.tool_execution_models import BaseToolResult, LoopDecision


_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD = 5


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    """启动根 Agent 执行循环请求。"""

    # 所属线程 ID。线程用于隔离同一任务内不同对话上下文。
    thread_id: str

    current_working_directory: str

    # 根 Agent 静态定义 ID。
    agent_definition_id: str

    # 本次执行使用的 LLM 服务商 ID。
    llm_provider_id: str

    # 本次执行使用的服务商模型接入项 ID。
    llm_model_binding_id: str

    # 用户输入内容块。纯文本场景可使用 from_text 构造。
    user_parts: tuple[UniPart, ...]

    # 用户输入内容块元数据。图片/PDF 场景调用方应传入准确页数等信息。
    user_parts_meta: tuple[PartMeta, ...]

    # 可选 task_id。不传时由 AgentRuntimeGateway 生成。
    task_id: str | None = None

    # 可选 root instance id。不传时由 AgentRuntimeGateway 生成。
    root_instance_id: str | None = None

    # 已有 conversation_id。为空时执行器新建 conversation。
    conversation_id: str | None = None

    # render_messages 透传选项。
    render_options: dict[str, Any] = field(default_factory=dict)

    # LLM 请求额外覆盖参数。
    request_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(
        cls,
        *,
        thread_id: str,
        current_working_directory: str,
        agent_definition_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
        user_text: str,
        task_id: str | None = None,
        root_instance_id: str | None = None,
        conversation_id: str | None = None,
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
    ) -> "AgentExecutionRequest":
        """创建纯文本用户输入的执行请求。"""
        return cls(
            thread_id=thread_id,
            current_working_directory=current_working_directory,
            agent_definition_id=agent_definition_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            user_parts=(TextPart(text=user_text),),
            user_parts_meta=(TextPartMeta(),),
            task_id=task_id,
            root_instance_id=root_instance_id,
            conversation_id=conversation_id,
            render_options=render_options or {},
            request_overrides=request_overrides or {},
        )


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """根 Agent 执行结果。"""

    # 任务 ID。
    task_id: str

    # 根 Agent 实例 ID。
    instance_id: str

    # 会话 ID。
    conversation_id: str

    # Trace 文件绝对路径。
    trace_file_path: str

    # 最后一条 assistant 消息。
    assistant_message: UniMessage | None


class AgentExecutionEngine:
    """真正 Agent 执行循环。

    引擎串联运行态管理、静态 Agent 定义、Prompt Snippet、Conversation、LLM 请求客户端和工具调度器。
    本类不提供 REST/CLI 管理接口，只作为程序内部运行入口。
    """

    def __init__(
        self,
        *,
        runtime_gateway: AgentRuntimeGateway,
        agent_definition_gateway: AgentDefinitionNorthGateway,
        llm_registry_gateway: LlmModelRegistryGateway,
        conversation_repository: ConversationRepository,
        prompt_snippet_gateway: PromptSnippetNorthGateway,
        local_tool_gateway: LocalToolGateway,
        mcp_client_gateway: McpClientGateway | None,
        skill_gateway: SkillGateway | None,
        llm_request_service: LlmRequestService,
        tool_dispatcher: AgentToolDispatcher,
        trace_recorder: AgentTraceRecorder,
    ) -> None:
        # Agent 运行态网关，负责 task/instance 创建和状态流转。
        self._runtime_gateway = runtime_gateway
        # 静态 Agent 定义网关，负责读取系统提示词、工具 allowlist、MCP allowlist 等执行边界。
        self._agent_definition_gateway = agent_definition_gateway
        # LLM 注册网关，负责解析 provider/binding/model metadata 为最终调用配置。
        self._llm_registry_gateway = llm_registry_gateway
        # Conversation 仓储，负责历史消息持久化。
        self._conversation_repository = conversation_repository
        # Prompt Snippet 网关，用于解析 AgentDefinition.system_prompt 中的 template_id。
        self._prompt_snippet_gateway = prompt_snippet_gateway
        self._local_tool_gateway = local_tool_gateway
        self._mcp_client_gateway = mcp_client_gateway
        self._skill_gateway = skill_gateway
        # 通用 LLM 请求服务，负责真实请求和响应归一化。
        self._llm_request_service = llm_request_service
        # 工具调度器，负责工具 schema 构建和工具调用执行。
        self._tool_dispatcher = tool_dispatcher
        # Agent 实例 Markdown Trace 记录器。
        self._trace_recorder = trace_recorder

    async def run_root_agent(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """创建 task + root Agent 实例并执行完整对话循环。"""
        task, instance = self._runtime_gateway.create_task_with_root_agent_instance(
            CreateTaskWithRootAgentInstanceRequest(
                thread_id=request.thread_id,
                current_working_directory=request.current_working_directory,
                agent_definition_id=request.agent_definition_id,
                llm_provider_id=request.llm_provider_id,
                llm_model_binding_id=request.llm_model_binding_id,
                task_id=request.task_id,
                root_instance_id=request.root_instance_id,
                conversation_id=request.conversation_id,
            )
        )
        trace_path = self._trace_recorder.start_trace(_trace_identity(instance))
        try:
            self._runtime_gateway.start_task(task.task_id)
            self._runtime_gateway.start_agent_instance(instance.instance_id)
            conversation = await self._load_or_create_conversation(instance, request)

            conversation.add_user_message(list(request.user_parts), list(request.user_parts_meta))
            user_message = conversation.messages[-1]
            await self._conversation_repository.save(conversation)
            self._trace_recorder.record_user_message(trace_path, user_message)

            resolved_model = self._llm_registry_gateway.resolve_provider_model(
                instance.llm_provider_id,
                instance.llm_model_binding_id,
            )

            plain_text_fallback_count = 0
            while True:
                agent_definition = self._load_agent_definition(instance.agent_definition_id)
                system_prompt = self._render_system_prompt(
                    agent_definition,
                    instance.current_working_directory,
                    request.render_options,
                )
                tools = await self._tool_dispatcher.build_llm_tools(
                    agent_definition=agent_definition,
                    sdk_platform=resolved_model.sdk_platform,
                )
                llm_response = await self._llm_request_service.send_prompt(
                    LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved_model,
                        system_prompt=system_prompt,
                        tools=tools,
                        render_options=request.render_options,
                        request_overrides=request.request_overrides,
                    )
                )
                assistant_message = llm_response.assistant_message
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_llm_response(trace_path, assistant_message)

                tool_uses = _extract_tool_uses(assistant_message)
                if not tool_uses:
                    plain_text_fallback_count += 1
                    self._trace_recorder.record_event(
                        trace_path,
                        "plain_text_fallback",
                        {
                            "fallback_count": plain_text_fallback_count,
                            "threshold": _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD,
                        },
                    )
                    if plain_text_fallback_count >= _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD:
                        self._runtime_gateway.pause_agent_instance(instance.instance_id)
                        self._runtime_gateway.pause_task(task.task_id)
                        self._trace_recorder.record_event(
                            trace_path,
                            "instance_paused",
                            "普通文本 fallback 连续达到阈值，Runtime 主动暂停任务。",
                        )
                        return AgentExecutionResult(
                            task_id=task.task_id,
                            instance_id=instance.instance_id,
                            conversation_id=conversation.id,
                            trace_file_path=str(trace_path),
                            assistant_message=assistant_message,
                        )
                    continue
                plain_text_fallback_count = 0

                tool_result_message, tool_results = await self._execute_tool_uses(
                    agent_definition,
                    tool_uses,
                )
                conversation.append_message(tool_result_message)
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_tool_result(trace_path, tool_result_message)
                loop_decision = _select_loop_decision(tool_results)
                if loop_decision != "continue":
                    self._apply_loop_decision(
                        loop_decision=loop_decision,
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        trace_path=trace_path,
                        tool_results=tool_results,
                    )
                    return AgentExecutionResult(
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        conversation_id=conversation.id,
                        trace_file_path=str(trace_path),
                        assistant_message=assistant_message,
                    )

            raise RuntimeError("Agent 执行循环异常结束。")
        except Exception as exc:
            failure_message = str(exc)
            self._runtime_gateway.fail_agent_instance(instance.instance_id, failure_message)
            self._runtime_gateway.fail_task(task.task_id, failure_message)
            self._trace_recorder.record_failure(trace_path, failure_message)
            raise

    def _load_agent_definition(self, agent_definition_id: str) -> AgentDefinition:
        """加载并校验 Agent 静态定义。"""
        definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if definition.disabled:
            raise ValueError(f"Agent 定义已禁用，不能执行: {agent_definition_id}")
        return definition

    async def _load_or_create_conversation(
        self,
        instance: AgentInstance,
        request: AgentExecutionRequest,
    ):
        """加载已有会话或为当前 Agent 实例创建新会话。"""
        if request.conversation_id:
            return await self._conversation_repository.get(request.conversation_id)
        conversation = create_conversation()
        await self._conversation_repository.create(
            conversation,
            task_id=instance.task_id,
            thread_id=instance.thread_id,
            agent_id=instance.agent_definition_id,
            agent_instance_id=instance.instance_id,
        )
        self._runtime_gateway.bind_agent_instance_conversation(
            instance.instance_id,
            conversation.id,
        )
        return conversation

    def _render_system_prompt(
        self,
        definition: AgentDefinition,
        current_working_directory: str,
        render_options: dict[str, Any],
    ) -> str | None:
        """按 AgentDefinition.system_prompt 顺序拼接最终系统提示词。"""
        segments: list[str] = []
        for segment in definition.system_prompt:
            if segment.text is not None:
                segments.append(segment.text)
            elif segment.template_id is not None:
                snippet = self._prompt_snippet_gateway.get_snippet(segment.template_id)
                segments.append(snippet.text)
        text = "\n\n".join(item for item in segments if item.strip())
        runtime_context_prompt = _build_runtime_context_prompt(current_working_directory)
        mcp_services_prompt = self._build_mcp_services_prompt(definition)
        skills_prompt = self._build_skills_prompt(definition)
        lifecycle_prompt = _build_lifecycle_runtime_prompt(render_options)
        lifecycle_tool_notice = self._build_lifecycle_tool_notice(definition)
        return "\n\n".join(
            item
            for item in (
                text,
                runtime_context_prompt,
                mcp_services_prompt,
                skills_prompt,
                lifecycle_prompt,
                lifecycle_tool_notice,
            )
            if item.strip()
        ) or None

    def _build_mcp_services_prompt(self, definition: AgentDefinition) -> str:
        if not definition.mcp_tools or self._mcp_client_gateway is None:
            return ""
        service_ids = []
        for binding in definition.mcp_tools:
            if binding.service_id not in service_ids:
                service_ids.append(binding.service_id)
        services = {
            item.service_id: item
            for item in self._mcp_client_gateway.list_services()
        }
        lines = [
            "# Agent 可用的 MCP 服务",
            "以下只列出当前 Agent 可用的 MCP 服务，不列出具体 MCP 工具权限。",
        ]
        for service_id in service_ids:
            service = services.get(service_id)
            if service is None:
                lines.append(f"- `{service_id}`：未找到服务配置。")
                continue
            description = service.description or ""
            lines.append(
                f"- `{service.service_id}`：name=`{service.name}`；description=`{description}`；"
                f"enabled=`{service.enabled}`；connection_status=`{service.connection_status}`；"
                f"group=`{service.group}`。"
            )
        return "\n".join(lines)

    def _build_skills_prompt(self, definition: AgentDefinition) -> str:
        if not definition.skill_ids or self._skill_gateway is None:
            return ""
        skills = {
            item.skill_id: item
            for item in self._skill_gateway.list_skills()
        }
        lines = [
            "# Agent 可用的 Skills",
            "以下只列出 Skill 描述和目录路径；如需详细说明，Agent 应根据需要读取对应目录下的 SKILL.md 和相关资源。",
        ]
        for skill_id in definition.skill_ids:
            skill = skills.get(skill_id)
            if skill is None:
                lines.append(f"- `{skill_id}`：未找到 Skill。")
                continue
            lines.append(
                f"- `{skill.skill_id}`：name=`{skill.name}`；description=`{skill.description}`；"
                f"directory_path=`{skill.directory_path}`。"
            )
        return "\n".join(lines)

    def _build_lifecycle_tool_notice(self, definition: AgentDefinition) -> str:
        tools = self._local_tool_gateway.list_tools(public_only=False, group_by=False)
        lifecycle_tool_names = [
            item.tool_name
            for item in tools
            if item.category == "lifecycle"
            and (item.always_enabled or item.tool_name in definition.tool_ids)
        ]
        if not lifecycle_tool_names:
            return ""
        names = "，".join(lifecycle_tool_names)
        return (
            "# 工具调用注意事项\n"
            f"- 禁止并发调用“{names}”这些工具，这些工具只能单独调用！这些工具只能单独调用！这些工具只能单独调用！"
        )

    async def _execute_tool_uses(
        self,
        definition: AgentDefinition,
        tool_uses: list[ToolUsePart],
    ) -> tuple[UniMessage, list[BaseToolResult]]:
        """执行 assistant 消息中的全部工具调用并生成 tool 消息。"""
        result_parts = []
        tool_results = []
        for tool_use in tool_uses:
            result = await self._tool_dispatcher.dispatch_tool_call(
                agent_definition=definition,
                tool_use=tool_use,
            )
            result_parts.append(result.result_part)
            tool_results.append(result.tool_result)
        return UniMessage(role=ROLE_TOOL, content=result_parts), tool_results

    def _apply_loop_decision(
        self,
        *,
        loop_decision: LoopDecision,
        task_id: str,
        instance_id: str,
        trace_path: Any,
        tool_results: list[BaseToolResult],
    ) -> None:
        """根据统一工具结果中的 loop_decision 更新运行态。"""
        details = [result.to_serializable_dict() for result in tool_results]
        if loop_decision == "pause":
            self._runtime_gateway.pause_agent_instance(instance_id)
            self._runtime_gateway.pause_task(task_id)
            self._trace_recorder.record_event(trace_path, "instance_paused", details)
            return
        if loop_decision == "complete":
            self._runtime_gateway.complete_agent_instance(instance_id)
            self._runtime_gateway.complete_task(task_id)
            self._trace_recorder.record_completed(trace_path)
            return
        if loop_decision == "terminate":
            self._runtime_gateway.cancel_agent_instance(instance_id)
            self._runtime_gateway.cancel_task(task_id)
            self._trace_recorder.record_event(trace_path, "instance_terminated", details)
            return
        if loop_decision == "fail":
            failure_message = _failure_message_from_tool_results(tool_results)
            self._runtime_gateway.fail_agent_instance(instance_id, failure_message)
            self._runtime_gateway.fail_task(task_id, failure_message)
            self._trace_recorder.record_failure(trace_path, failure_message)
            return
        raise ValueError(f"未知循环决策: {loop_decision}")


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    """从 assistant 消息中提取工具调用。"""
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _build_runtime_context_prompt(current_working_directory: str) -> str:
    working_directory = Path(current_working_directory).expanduser()
    resolved_working_directory = working_directory.resolve(strict=False)
    lines = [
        "# Runtime 执行环境",
        f"- Current working directory: `{resolved_working_directory}`",
        f"- Operating system: `{platform.system()}`",
        f"- OS release: `{platform.release()}`",
        f"- OS version: `{platform.version()}`",
        f"- Machine: `{platform.machine()}`",
        f"- Python platform: `{platform.platform()}`",
    ]
    git_roots = _discover_git_roots_under(resolved_working_directory)
    if git_roots:
        lines.append("")
        lines.append("# 当前工作目录下的 Git 仓库根目录")
        for git_root in git_roots:
            lines.append(f"- `{git_root}`")
    return "\n".join(lines)


def _discover_git_roots_under(root: Path) -> list[str]:
    if not root.exists() or not root.is_dir():
        return []
    discovered: list[str] = []
    for current_root, dir_names, _file_names in os.walk(root):
        current_path = Path(current_root)
        if ".git" in dir_names:
            discovered.append(str(current_path))
            dir_names.remove(".git")
        excluded = {
            ".hg",
            ".svn",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "node_modules",
            ".venv",
            "venv",
        }
        dir_names[:] = [
            name
            for name in dir_names
            if name not in excluded
        ]
    return sorted(discovered)


def _build_lifecycle_runtime_prompt(render_options: dict[str, Any]) -> str:
    channel = str(
        render_options.get("channel")
        or render_options.get("frontend_channel")
        or "unknown"
    )
    supported_formats = _format_render_option_values(
        render_options.get("supported_text_formats")
        or render_options.get("text_formats")
        or ("markdown", "plain_text", "json")
    )
    preferred_format = str(
        render_options.get("preferred_text_format")
        or render_options.get("text_format")
        or "markdown"
    )
    return (
        "## Runtime lifecycle tool policy\n"
        "- Use the `agent_lifecycle` tool for all user-facing communication instead of plain assistant text.\n"
        "- Use `notify` for non-blocking progress updates, `ask_user` for questions/confirmation/selection, "
        "`pause` to wait, `complete` when the task is finished, `terminate` when the task should be cancelled, "
        "and `fail` when execution cannot recover.\n"
        "- Do not call lifecycle communication tools in the same assistant turn as business tools unless the "
        "user explicitly requires it.\n"
        f"- Current frontend channel: `{channel}`.\n"
        f"- Supported text formats: `{supported_formats}`.\n"
        f"- Preferred text format: `{preferred_format}`.\n"
        "- Set `text_format` explicitly and do not return HTML.\n"
        "- `ask_user.options` must be objects with `id`, `label`, and optional `description`."
    )


def _format_render_option_values(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _trace_identity(instance: AgentInstance) -> AgentTraceIdentity:
    """从 AgentInstance 构造 Trace 文件身份信息。"""
    return AgentTraceIdentity(
        task_id=instance.task_id,
        instance_id=instance.instance_id,
        thread_id=instance.thread_id,
        agent_definition_id=instance.agent_definition_id,
        trace_id=instance.trace_id,
    )


def _select_loop_decision(tool_results: list[BaseToolResult]) -> LoopDecision:
    """从一组工具结果中选择本轮最终循环决策。"""
    priority: dict[LoopDecision, int] = {
        "continue": 0,
        "pause": 1,
        "complete": 2,
        "terminate": 3,
        "fail": 4,
    }
    selected: LoopDecision = "continue"
    for result in tool_results:
        decision = result.loop_decision
        if priority[decision] > priority[selected]:
            selected = decision
    return selected


def _failure_message_from_tool_results(tool_results: list[BaseToolResult]) -> str:
    """从工具结果中提取失败原因。"""
    for result in tool_results:
        if result.error_message:
            return result.error_message
        if result.loop_decision == "fail" and isinstance(result.output, str):
            return result.output
    return "工具返回 fail 循环决策。"
