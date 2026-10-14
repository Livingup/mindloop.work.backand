# -*- coding: utf-8 -*-
"""Agent 定义领域实体。

Agent 定义是 Agent Runtime 中描述“某类 Agent 是什么、能用什么能力”的静态模板数据。
每个定义以 agent_id 作为业务标识，并使用 agent_id 作为 YAML 文件名主名持久化。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# agent_id 同时作为 YAML 文件名主名，必须避免路径穿越、隐藏文件和跨平台保留名问题。
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Agent 分组用于管理后台和 Agent 发现时的业务分类，保持文件夹/URL 友好的简单格式。
_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Windows 保留设备名不允许作为文件名主名，避免在 Windows 10 环境中无法落盘。
_WINDOWS_RESERVED_FILE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class SystemPromptSegment:
    """系统提示词片段。

    Agent 的最终系统提示词由多个片段按数组顺序拼接得到。片段支持两种业务形态：
    1. text：直接写在 Agent 定义中的固定提示词文本；
    2. template_id：引用 prompt_template 模块中维护的可复用提示词片段。

    二者必须且只能填写一个，确保运行时拼接系统提示词时不存在歧义。
    """

    # 直接提示词文本。用于表达只服务于当前 Agent 的专属规则。
    text: str | None = None

    # 可复用提示词片段 ID。运行时由提示词模板服务按该 ID 解析为文本。
    template_id: str | None = None

    def __post_init__(self) -> None:
        """校验系统提示词片段只能是 text 或 template_id 二选一。"""
        has_text = self.text is not None
        has_template = self.template_id is not None
        if has_text == has_template:
            raise ValueError("系统提示词片段必须且只能包含 text 或 template_id 之一。")
        if has_text and not self.text.strip():
            raise ValueError("系统提示词 text 片段不能为空。")
        if has_template:
            template_id = self.template_id or ""
            if not template_id.strip():
                raise ValueError("系统提示词 template_id 不能为空。")
            if "/" in template_id or "\\" in template_id:
                raise ValueError("系统提示词 template_id 不能包含路径分隔符。")

    def to_dict(self) -> dict[str, str]:
        """转换为 YAML 友好的字典结构。"""
        if self.text is not None:
            return {"text": self.text}
        return {"template_id": self.template_id or ""}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemPromptSegment:
        """从 YAML 字典创建系统提示词片段。"""
        return cls(text=data.get("text"), template_id=data.get("template_id"))


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    """Agent 可用的 MCP 工具绑定。

    Agent 定义只记录“允许使用哪个 MCP 服务下的哪些工具”，不直接保存远程工具 schema。
    工具 schema 属于 MCP 服务运行时发现结果，后续由 Agent 执行引擎按 service_id + tool_id 动态获取。
    """

    # MCP 服务定义 ID，对应 mcp_client 模块中的 service_id。
    service_id: str

    # 当前 Agent 被允许调用的该 MCP 服务工具 ID 列表。
    tool_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """校验 MCP 工具绑定结构完整且工具 ID 不重复。"""
        if not self.service_id or not self.service_id.strip():
            raise ValueError("MCP 工具绑定 service_id 不能为空。")
        if "/" in self.service_id or "\\" in self.service_id:
            raise ValueError("MCP 工具绑定 service_id 不能包含路径分隔符。")
        if not self.tool_ids:
            raise ValueError("MCP 工具绑定 tool_ids 不能为空。")
        _ensure_unique_non_empty_ids(self.tool_ids, "MCP 工具 ID")

    def to_dict(self) -> dict[str, Any]:
        """转换为 YAML 友好的字典结构。"""
        return {"service_id": self.service_id, "tool_ids": list(self.tool_ids)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpToolBinding:
        """从 YAML 字典创建 MCP 工具绑定。"""
        return cls(
            service_id=str(data.get("service_id", "")),
            tool_ids=tuple(str(item) for item in data.get("tool_ids", []) or []),
        )


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Agent 定义聚合根。

    该聚合根描述一个 Agent 模板的静态能力边界：系统提示词、可用本地工具、MCP 工具、Skill、
    可用子 Agent，以及管理后台所需的展示名、说明、分组、内置后台标记和禁用状态。

    业务约束：
    - agent_id 是全局唯一业务标识，同时是 YAML 文件名主名，必须满足跨平台安全文件名格式；
    - system_prompt 允许 text 与 template_id 片段交叉混合，并保持定义顺序；
    - 本地工具、Skill、子 Agent、MCP 工具列表内部不允许重复；
    - 可用子 Agent 不能包含自身，避免直接自引用。
    """

    # Agent 定义唯一标识，同时作为 ~/.spread-ai/agents/{agent_id}.yaml 的文件名主名。
    agent_id: str

    # Agent 展示名。用于管理后台、CLI 列表和 Agent 选择界面展示，可为空。
    name: str | None = None

    # Agent 用途说明。用于帮助用户或其他 Agent 判断“什么时候应该使用该 Agent”。
    description: str | None = None

    # 系统提示词片段序列。运行时按数组顺序拼接 text 片段与 template_id 引用片段。
    system_prompt: tuple[SystemPromptSegment, ...] = field(default_factory=tuple)

    # 当前 Agent 被允许使用的本地工具 ID 列表，对应 tool 模块的 tool_name。
    tool_ids: tuple[str, ...] = field(default_factory=tuple)

    # 当前 Agent 被允许使用的 MCP 服务工具绑定列表。
    mcp_tools: tuple[McpToolBinding, ...] = field(default_factory=tuple)

    # 当前 Agent 可加载的 Skill ID 列表，对应 skill 模块的 skill_id。
    skill_ids: tuple[str, ...] = field(default_factory=tuple)

    # 当前 Agent 可调度的子 Agent 定义 ID 列表。
    available_sub_agent_ids: tuple[str, ...] = field(default_factory=tuple)

    default_llm_policy_group_id: str | None = None

    allowed_llm_policy_group_ids: tuple[str, ...] = field(default_factory=tuple)

    # Agent 管理分组。用于管理后台按业务域筛选 Agent 定义。
    group: str = "default"

    # 是否为系统内置后台 Agent。该标记用于区分普通用户可选 Agent 与运行时内部后台 Agent。
    builtin_background_agent: bool = False

    # 是否禁用。禁用后定义仍保留，但默认不应参与用户选择和执行调度。
    disabled: bool = False

    def __post_init__(self) -> None:
        """实体创建时校验 Agent 定义的核心业务规则。"""
        _validate_agent_id(self.agent_id)
        _validate_optional_text(self.name, "Agent 展示名")
        _validate_optional_text(self.description, "Agent 用途说明")
        _validate_group(self.group)
        _ensure_unique_non_empty_ids(self.tool_ids, "本地工具 ID")
        _ensure_unique_non_empty_ids(self.skill_ids, "Skill ID")
        _ensure_unique_non_empty_ids(self.available_sub_agent_ids, "可用子 Agent ID")
        _ensure_unique_non_empty_ids(self.allowed_llm_policy_group_ids, "允许 LLM 执行策略组 ID")
        if self.default_llm_policy_group_id is not None:
            _ensure_unique_non_empty_ids((self.default_llm_policy_group_id,), "默认 LLM 执行策略组 ID")
            if (
                self.allowed_llm_policy_group_ids
                and self.default_llm_policy_group_id not in self.allowed_llm_policy_group_ids
            ):
                raise ValueError("默认 LLM 执行策略组必须包含在允许 LLM 执行策略组列表中。")
        for sub_agent_id in self.available_sub_agent_ids:
            _validate_agent_id(sub_agent_id)
        if self.agent_id in self.available_sub_agent_ids:
            raise ValueError("可用子 Agent 列表不能包含当前 Agent 自身。")
        _ensure_unique_service_ids(self.mcp_tools)

    def to_dict(self) -> dict[str, Any]:
        """将 Agent 定义转换为 YAML 友好的字典结构。"""
        data: dict[str, Any] = {
            "agent_id": self.agent_id,
            "group": self.group,
            "system_prompt": [segment.to_dict() for segment in self.system_prompt],
            "tool_ids": list(self.tool_ids),
            "mcp_tools": [binding.to_dict() for binding in self.mcp_tools],
            "skill_ids": list(self.skill_ids),
            "available_sub_agent_ids": list(self.available_sub_agent_ids),
            "allowed_llm_policy_group_ids": list(self.allowed_llm_policy_group_ids),
            "builtin_background_agent": self.builtin_background_agent,
            "disabled": self.disabled,
        }
        if self.default_llm_policy_group_id is not None:
            data["default_llm_policy_group_id"] = self.default_llm_policy_group_id
        if self.name is not None:
            data["name"] = self.name
        if self.description is not None:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, agent_id: str, data: dict[str, Any]) -> AgentDefinition:
        """从 YAML 字典创建 Agent 定义实体。

        Args:
            agent_id: Agent 定义 ID，通常来自文件名；当 YAML 内声明 agent_id 时必须与文件名一致。
            data: YAML 文件解析后的字典。
        """
        yaml_agent_id = data.get("agent_id", agent_id)
        if yaml_agent_id != agent_id:
            raise ValueError(
                f"Agent 定义文件名 ID '{agent_id}' 与 YAML 内 agent_id '{yaml_agent_id}' 不一致。"
            )

        prompt_items = data.get("system_prompt", []) or []
        if not isinstance(prompt_items, list):
            raise ValueError("system_prompt 必须是数组。")
        mcp_items = data.get("mcp_tools", []) or []
        if not isinstance(mcp_items, list):
            raise ValueError("mcp_tools 必须是数组。")

        return cls(
            agent_id=agent_id,
            name=data.get("name"),
            description=data.get("description"),
            group=str(data.get("group", "default")),
            system_prompt=tuple(
                SystemPromptSegment.from_dict(_require_dict(item, "system_prompt 元素"))
                for item in prompt_items
            ),
            tool_ids=_to_str_tuple(data.get("tool_ids", []), "tool_ids"),
            mcp_tools=tuple(
                McpToolBinding.from_dict(_require_dict(item, "mcp_tools 元素"))
                for item in mcp_items
            ),
            skill_ids=_to_str_tuple(data.get("skill_ids", []), "skill_ids"),
            available_sub_agent_ids=_to_str_tuple(
                data.get("available_sub_agent_ids", []), "available_sub_agent_ids"
            ),
            default_llm_policy_group_id=data.get("default_llm_policy_group_id"),
            allowed_llm_policy_group_ids=_to_str_tuple(
                data.get("allowed_llm_policy_group_ids", []),
                "allowed_llm_policy_group_ids",
            ),
            builtin_background_agent=bool(data.get("builtin_background_agent", False)),
            disabled=bool(data.get("disabled", False)),
        )


def _validate_agent_id(agent_id: str) -> None:
    """校验 agent_id 是否满足跨平台安全文件名规则。"""
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id 不能为空。")
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise ValueError(
            "agent_id 只能包含英文字母、数字、下划线和中划线，"
            "必须以英文字母或数字开头，长度不超过 64。"
        )
    if agent_id.upper() in _WINDOWS_RESERVED_FILE_NAMES:
        raise ValueError(f"agent_id 不能使用 Windows 保留文件名: {agent_id}。")


def _validate_group(group: str) -> None:
    """校验 Agent 分组名。"""
    if not group or not group.strip():
        raise ValueError("Agent 分组不能为空。")
    if not _GROUP_PATTERN.match(group):
        raise ValueError("Agent 分组只能包含英文字母、数字、下划线和中划线，长度不超过 64。")


def _validate_optional_text(value: str | None, field_name: str) -> None:
    """校验可选展示文本：允许不填，但不允许只填空白。"""
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} 不能是空白字符串。")


def _ensure_unique_non_empty_ids(ids: tuple[str, ...], field_name: str) -> None:
    """校验 ID 列表中每个值非空且不重复。"""
    seen: set[str] = set()
    for item in ids:
        if not item or not item.strip():
            raise ValueError(f"{field_name} 不能为空。")
        if "/" in item or "\\" in item:
            raise ValueError(f"{field_name} 不能包含路径分隔符: {item}。")
        if item in seen:
            raise ValueError(f"{field_name} 不能重复: {item}。")
        seen.add(item)


def _ensure_unique_service_ids(bindings: tuple[McpToolBinding, ...]) -> None:
    """校验同一个 Agent 定义中 MCP 服务绑定不重复。"""
    seen: set[str] = set()
    for binding in bindings:
        if binding.service_id in seen:
            raise ValueError(f"MCP 服务绑定不能重复: {binding.service_id}。")
        seen.add(binding.service_id)


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    """要求 YAML 数组元素必须是对象结构。"""
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} 必须是对象结构。")
    return value


def _to_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    """将 YAML 数组字段转换为字符串元组。"""
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组。")
    return tuple(str(item) for item in value)
