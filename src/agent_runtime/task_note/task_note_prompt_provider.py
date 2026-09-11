# -*- coding: utf-8 -*-
"""Agent Task Note 提示词与工具提供器。

负责：
1. 从提示词模板服务（PromptSnippetNorthGateway）动态读取 note 更新系统提示词模板 system_note_update；
2. 若未预先注册，自动从内置模板文件（system_note_update.md）加载并向网关注册持久化；
3. 将标准变量（如 {{agent_instance_id}} 和 {{available_tools}}）渲染为针对当前 Agent 实例与 32 个 Note 工具的系统提示词；
4. 按调用方请求的 API Format 构建 32 个 Note 工具的 LlmToolDefinition；
5. 提供触发压缩时的边界指令提示词以及会话替换时的恢复引导提示词。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetNorthGateway,
)
from agent_runtime.prompt_template.prompt_variables import (
    PromptVariableContext,
    build_prompt_variable_values,
    render_prompt_variables,
)
from agent_runtime.tool.north_gateway.local_tool_contracts import LocalToolGateway

logger = logging.getLogger(__name__)

NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID = "system_note_update"
NOTE_UPDATE_SNIPPET_CATEGORY = "system"

TASK_NOTE_TOOL_NAMES: tuple[str, ...] = (
    "append_mission_prompt",
    "batch_create_task_todos",
    "bind_resources_to_mission",
    "create_mission",
    "create_task_artifact_other",
    "create_task_decision",
    "create_task_discovery",
    "create_task_external_state",
    "create_task_issue",
    "create_task_todo",
    "create_task_validation",
    "create_task_workspace_context",
    "delete_task_artifact_other",
    "delete_task_discovery",
    "delete_task_todo",
    "delete_task_workspace_context",
    "note_update_end",
    "resolve_task_issue",
    "supersede_task_decision",
    "supersede_task_solution",
    "update_mission_requirement",
    "update_mission_status",
    "update_task_decision",
    "update_task_discovery",
    "update_task_external_state",
    "update_task_issue",
    "update_task_progress",
    "update_task_solution",
    "update_task_todo_content",
    "update_task_todo_status",
    "update_task_validation",
    "update_task_workspace_context",
)


class TaskNotePromptProvider:
    """Task Note 提示词与工具定义提供器。"""

    def __init__(
        self,
        *,
        prompt_snippet_gateway: PromptSnippetNorthGateway | None = None,
        local_tool_gateway: LocalToolGateway | None = None,
    ) -> None:
        self._prompt_snippet_gateway = prompt_snippet_gateway
        self._local_tool_gateway = local_tool_gateway
        self._default_template_path = (
            Path(__file__).resolve().parent / "prompts" / "system_note_update.md"
        )

    def get_note_tool_names(self) -> tuple[str, ...]:
        """获取所有 32 个 Note 专属工具名称列表。"""
        if self._local_tool_gateway is not None:
            try:
                tools = self._local_tool_gateway.list_tools(public_only=False)
                discovered = [t.tool_name for t in tools if t.group == "task_note"]
                if discovered:
                    return tuple(sorted(discovered))
            except Exception as exc:
                logger.warning("从 LocalToolGateway 发现 task_note 工具异常，回退内置列表: %s", exc)
        return TASK_NOTE_TOOL_NAMES

    def get_note_tools(self, api_format: str) -> tuple[LlmToolDefinition, ...]:
        """按目标 api_format 构建 32 个 Note 工具的 LlmToolDefinition。"""
        if self._local_tool_gateway is None:
            raise RuntimeError("LocalToolGateway 未注入，无法获取 Note 工具 Schema。")
        tool_names = self.get_note_tool_names()
        llm_tools: list[LlmToolDefinition] = []
        for name in tool_names:
            detail = self._local_tool_gateway.get_tool(name)
            schema = detail.schemas.get(api_format)
            if schema is None:
                raise ValueError(f"Note 工具 {name} 缺少 {api_format} 格式的 schema。")
            llm_tools.append(LlmToolDefinition(name=name, schema=schema, source="local"))
        return tuple(llm_tools)

    def load_raw_template(self) -> str:
        """从模板网关或内置模板文件加载未渲染的系统提示词模板。"""
        if self._prompt_snippet_gateway is not None:
            try:
                snippet = self._prompt_snippet_gateway.get_snippet(NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID)
                if snippet and snippet.text:
                    return snippet.text
            except KeyError:
                logger.info(
                    "提示词模板 %s 未在网关注册，准备从内置文件加载并注册。",
                    NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID,
                )
            except Exception as exc:
                logger.warning("从提示词模板网关读取模板失败: %s，回退到内置文件", exc)

        # 从内置文件加载
        if not self._default_template_path.exists():
            raise FileNotFoundError(f"内置 Note 更新提示词模板不存在: {self._default_template_path}")
        raw_text = self._default_template_path.read_text(encoding="utf-8")

        # 尝试自动在网关中注册以持久化
        if self._prompt_snippet_gateway is not None:
            try:
                self._prompt_snippet_gateway.create_snippet(
                    PromptSnippetCreateRequest(
                        name=NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID,
                        text=raw_text,
                        category=NOTE_UPDATE_SNIPPET_CATEGORY,
                    )
                )
                logger.info("已自动向模板网关注册 %s 提示词模板。", NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID)
            except Exception as exc:
                logger.warning("自动向模板网关注册 %s 失败（忽略）: %s", NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID, exc)

        return raw_text

    def render_note_system_prompt(
        self,
        *,
        agent_instance_id: str,
        agent_name: str = "",
        current_working_directory: str = "",
        available_tools: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        """渲染 Note 更新状态下的完整系统提示词。"""
        template_text = self.load_raw_template()
        tools = available_tools if available_tools is not None else self.get_note_tool_names()
        context = PromptVariableContext(
            agent_instance_id=agent_instance_id,
            agent_name=agent_name or agent_instance_id,
            current_working_directory=current_working_directory or os.getcwd(),
            available_tools=tuple(tools),
        )
        variable_values = build_prompt_variable_values(context)
        return render_prompt_variables(template_text, variable_values)

    def build_compression_boundary_prompt(self, note_snapshot: str | dict[str, Any]) -> str:
        """生成触发上下文压缩时的边界 User 指令提示词（注入基线快照）。"""
        snapshot_str = (
            note_snapshot
            if isinstance(note_snapshot, str)
            else json.dumps(note_snapshot, ensure_ascii=False, indent=2)
        )
        return (
            "上面的历史消息是之前的业务agent的对话记录。你现在的状态是note agent，上下文空间快不足了，请立即更新业务agent的note。"
            f"下面是目标业务agent的当前note完整快照：{snapshot_str}。请更新由你负责的note部分。"
        )

    def build_resumed_conversation_prompt(self, note_snapshot: str | dict[str, Any]) -> str:
        """生成 Note 更新完成后全新会话的业务唤醒提示词。"""
        snapshot_str = (
            note_snapshot
            if isinstance(note_snapshot, str)
            else json.dumps(note_snapshot, ensure_ascii=False, indent=2)
        )
        return (
            "系统已完成上下文压缩与Note更新，并切换至全新的干净会话。你的运行态已恢复为业务Agent。\n"
            f"以下是为你恢复的最新完整Note快照：\n{snapshot_str}\n\n"
            "请根据Note中的任务状态、进展与下一步计划，继续执行并推进用户业务任务。"
        )