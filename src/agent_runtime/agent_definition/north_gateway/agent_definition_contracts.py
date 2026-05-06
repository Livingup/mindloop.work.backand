# -*- coding: utf-8 -*-
"""Agent 定义管理北向网关契约。

该模块定义 Agent 定义服务对外暴露的请求模型、结果模型和服务接口。
所有调用方（程序内部、REST API、CLI、后续 Agent 执行引擎）均依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime.agent_definition.agent_definition import (
    AgentDefinition,
    McpToolBinding,
    SystemPromptSegment,
)


@dataclass(frozen=True, slots=True)
class AgentDefinitionCreateRequest:
    """创建 Agent 定义请求。

    调用方提交完整 Agent 静态模板数据，由服务校验 agent_id 唯一性后持久化为 YAML 文件。
    """

    # Agent 定义唯一标识，同时也是 YAML 文件名主名。
    agent_id: str

    # Agent 展示名。用于管理后台、CLI 列表和 Agent 选择界面展示，可不填。
    name: str | None = None

    # Agent 用途说明。用于说明该 Agent 做什么、什么时候应该使用，可不填。
    description: str | None = None

    # Agent 管理分组。用于按业务域组织 Agent 定义。
    group: str = "default"

    # 系统提示词片段序列，支持 text 与 template_id 交叉混合。
    system_prompt: tuple[SystemPromptSegment, ...] = field(default_factory=tuple)

    # 本地工具 ID 列表。
    tool_ids: tuple[str, ...] = field(default_factory=tuple)

    # MCP 服务工具绑定列表。
    mcp_tools: tuple[McpToolBinding, ...] = field(default_factory=tuple)

    # Skill ID 列表。
    skill_ids: tuple[str, ...] = field(default_factory=tuple)

    # 可用子 Agent 定义 ID 列表。
    available_sub_agent_ids: tuple[str, ...] = field(default_factory=tuple)

    default_llm_policy_group_id: str | None = None

    allowed_llm_policy_group_ids: tuple[str, ...] = field(default_factory=tuple)

    # 是否为系统内置后台 Agent。
    builtin_background_agent: bool = False

    # 是否禁用。
    disabled: bool = False

    def to_definition(self) -> AgentDefinition:
        """转换为 AgentDefinition 聚合根。"""
        return AgentDefinition(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            group=self.group,
            system_prompt=self.system_prompt,
            tool_ids=self.tool_ids,
            mcp_tools=self.mcp_tools,
            skill_ids=self.skill_ids,
            available_sub_agent_ids=self.available_sub_agent_ids,
            default_llm_policy_group_id=self.default_llm_policy_group_id,
            allowed_llm_policy_group_ids=self.allowed_llm_policy_group_ids,
            builtin_background_agent=self.builtin_background_agent,
            disabled=self.disabled,
        )

    @classmethod
    def from_definition(cls, definition: AgentDefinition) -> AgentDefinitionCreateRequest:
        """从 AgentDefinition 聚合根创建请求对象。"""
        return cls(
            agent_id=definition.agent_id,
            name=definition.name,
            description=definition.description,
            group=definition.group,
            system_prompt=definition.system_prompt,
            tool_ids=definition.tool_ids,
            mcp_tools=definition.mcp_tools,
            skill_ids=definition.skill_ids,
            available_sub_agent_ids=definition.available_sub_agent_ids,
            default_llm_policy_group_id=definition.default_llm_policy_group_id,
            allowed_llm_policy_group_ids=definition.allowed_llm_policy_group_ids,
            builtin_background_agent=definition.builtin_background_agent,
            disabled=definition.disabled,
        )


@dataclass(frozen=True, slots=True)
class AgentDefinitionUpdateRequest:
    """更新 Agent 定义请求。

    所有字段均可选；值为 None 表示保持原值不变。列表字段如果传入空元组，表示明确清空该能力列表。
    """

    # 新展示名。None 表示不修改。
    name: str | None = None

    # 新用途说明。None 表示不修改。
    description: str | None = None

    # 新分组。None 表示不修改。
    group: str | None = None

    # 新系统提示词片段序列。None 表示不修改，空元组表示清空系统提示词。
    system_prompt: tuple[SystemPromptSegment, ...] | None = None

    # 新本地工具 ID 列表。None 表示不修改，空元组表示清空。
    tool_ids: tuple[str, ...] | None = None

    # 新 MCP 工具绑定列表。None 表示不修改，空元组表示清空。
    mcp_tools: tuple[McpToolBinding, ...] | None = None

    # 新 Skill ID 列表。None 表示不修改，空元组表示清空。
    skill_ids: tuple[str, ...] | None = None

    # 新可用子 Agent ID 列表。None 表示不修改，空元组表示清空。
    available_sub_agent_ids: tuple[str, ...] | None = None

    default_llm_policy_group_id: str | None = None
    update_default_llm_policy_group_id: bool = False

    allowed_llm_policy_group_ids: tuple[str, ...] | None = None

    # 新系统内置后台 Agent 标记。None 表示不修改。
    builtin_background_agent: bool | None = None

    # 新禁用标记。None 表示不修改。
    disabled: bool | None = None

    def apply_to(self, current: AgentDefinition) -> AgentDefinition:
        """将部分更新应用到现有 AgentDefinition，返回新的聚合根。"""
        return AgentDefinition(
            agent_id=current.agent_id,
            name=current.name if self.name is None else self.name,
            description=(
                current.description if self.description is None else self.description
            ),
            group=current.group if self.group is None else self.group,
            system_prompt=(
                current.system_prompt if self.system_prompt is None else self.system_prompt
            ),
            tool_ids=current.tool_ids if self.tool_ids is None else self.tool_ids,
            mcp_tools=current.mcp_tools if self.mcp_tools is None else self.mcp_tools,
            skill_ids=current.skill_ids if self.skill_ids is None else self.skill_ids,
            available_sub_agent_ids=(
                current.available_sub_agent_ids
                if self.available_sub_agent_ids is None
                else self.available_sub_agent_ids
            ),
            default_llm_policy_group_id=(
                current.default_llm_policy_group_id
                if not self.update_default_llm_policy_group_id
                else self.default_llm_policy_group_id
            ),
            allowed_llm_policy_group_ids=(
                current.allowed_llm_policy_group_ids
                if self.allowed_llm_policy_group_ids is None
                else self.allowed_llm_policy_group_ids
            ),
            builtin_background_agent=(
                current.builtin_background_agent
                if self.builtin_background_agent is None
                else self.builtin_background_agent
            ),
            disabled=current.disabled if self.disabled is None else self.disabled,
        )


class AgentDefinitionNorthGateway(Protocol):
    """Agent 定义管理北向网关接口。"""

    def list_agent_definitions(
        self,
        *,
        group: str | None = None,
        include_disabled: bool = True,
    ) -> list[AgentDefinition]:
        """列出 Agent 定义，可按分组和禁用状态筛选。"""
        ...

    def get_agent_definition(self, agent_id: str) -> AgentDefinition:
        """根据 agent_id 获取 Agent 定义。未找到时抛出 KeyError。"""
        ...

    def create_agent_definition(
        self,
        request: AgentDefinitionCreateRequest,
    ) -> AgentDefinition:
        """创建 Agent 定义。agent_id 已存在时抛出 ValueError。"""
        ...

    def update_agent_definition(
        self,
        agent_id: str,
        request: AgentDefinitionUpdateRequest,
    ) -> AgentDefinition:
        """更新 Agent 定义。目标定义不存在时抛出 KeyError。"""
        ...

    def delete_agent_definition(self, agent_id: str) -> None:
        """删除 Agent 定义。不存在时静默跳过。"""
        ...

    def enable_agent_definition(self, agent_id: str) -> AgentDefinition:
        """启用 Agent 定义。"""
        ...

    def disable_agent_definition(self, agent_id: str) -> AgentDefinition:
        """禁用 Agent 定义。"""
        ...
