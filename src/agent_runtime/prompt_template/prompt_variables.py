# -*- coding: utf-8 -*-
"""系统提示词统一变量模块。

负责管理系统预置变量元数据定义（占位符、键名、含义说明、取值逻辑说明），
以及在运行时从上下文中提取变量值并执行系统提示词模板占位符的动态替换。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("agent_runtime.prompt_template.prompt_variables")

_VARIABLE_REGEX = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")


@dataclass(frozen=True, slots=True)
class PromptVariableDefinition:
    """系统预置变量定义实体。

    属性:
        placeholder: 占位符字符串，例如 "{{agent_instance_id}}"。
        name: 变量标识键名，例如 "agent_instance_id"。
        description: 含义说明字段（面向用户说明该变量的业务用途）。
        value_logic: 取值逻辑说明字段（面向用户说明该变量是如何获取或计算的）。
        category: 分类标识（例如 instance, environment, learning, capability, lifecycle）。
    """

    placeholder: str
    name: str
    description: str
    value_logic: str
    category: str = "general"

    def to_dict(self) -> dict[str, str]:
        return {
            "placeholder": self.placeholder,
            "name": self.name,
            "description": self.description,
            "value_logic": self.value_logic,
            "category": self.category,
        }


BUILTIN_PROMPT_VARIABLES: tuple[PromptVariableDefinition, ...] = (
    PromptVariableDefinition(
        placeholder="{{agent_instance_id}}",
        name="agent_instance_id",
        description="当前 Agent 运行时的唯一实例 ID",
        value_logic="由运行时在 Agent 实例创建时分配的唯一会话实例句柄",
        category="instance",
    ),
    PromptVariableDefinition(
        placeholder="{{agent_name}}",
        name="agent_name",
        description="当前 Agent 定义的友好显示名称",
        value_logic="取自 AgentDefinition.name，未配置时取 agent_id",
        category="instance",
    ),
    PromptVariableDefinition(
        placeholder="{{parent_agent_instance_id}}",
        name="parent_agent_instance_id",
        description="创建本 Agent 的父 Agent 实例 ID",
        value_logic="若当前为子 Agent，取父级实例句柄；若是根 Agent 则为空字符串",
        category="instance",
    ),
    PromptVariableDefinition(
        placeholder="{{current_working_directory}}",
        name="current_working_directory",
        description="当前 Agent 执行的工作区真实绝对路径",
        value_logic="由运行时获取当前工作空间路径并展开解析为标准化绝对路径",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{git_repo_roots}}",
        name="git_repo_roots",
        description="工作区内扫描出的 Git 仓库根目录 Markdown 列表",
        value_logic="在当前工作目录下递归扫描包含 .git 的目录并格式化为列表文本（未找到则为空）",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{os_platform}}",
        name="os_platform",
        description="宿主操作系统平台名称",
        value_logic="通过 Python platform.system() 动态获取（如 Windows、Linux、Darwin）",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{os_release}}",
        name="os_release",
        description="操作系统内核或发行版本号",
        value_logic="通过 Python platform.release() 动态获取（如 10.0.26100）",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{os_version}}",
        name="os_version",
        description="操作系统详细版本号",
        value_logic="通过 Python platform.version() 动态获取",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{system_architecture}}",
        name="system_architecture",
        description="宿主机器 CPU 硬件架构",
        value_logic="通过 Python platform.machine() 动态获取（如 AMD64、arm64）",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{python_version}}",
        name="python_version",
        description="宿主 Python 运行时版本",
        value_logic="通过 Python platform.python_version() 动态获取（如 3.11.9）",
        category="environment",
    ),
    PromptVariableDefinition(
        placeholder="{{dedicated_skill_file}}",
        name="dedicated_skill_file",
        description="当前 Agent 专属 SKILL.md 文件绝对路径",
        value_logic="由系统 Skill 根目录与当前 Agent ID 拼接计算得到",
        category="learning",
    ),
    PromptVariableDefinition(
        placeholder="{{dedicated_memory_file}}",
        name="dedicated_memory_file",
        description="当前 Agent 专属 memory.md 项目记忆文件绝对路径",
        value_logic="由当前工作目录下的 .spread-ai/memory 与当前 Agent ID 拼接计算得到",
        category="learning",
    ),
    PromptVariableDefinition(
        placeholder="{{skill_root_directory}}",
        name="skill_root_directory",
        description="通用 Skill 技能存储根目录绝对路径",
        value_logic="取自系统配置中的 skills_dir 配置项标准化路径",
        category="learning",
    ),
    PromptVariableDefinition(
        placeholder="{{available_tools}}",
        name="available_tools",
        description="当前 Agent 实际可调用的 LLM 工具名称数组",
        value_logic="提取当前会话实际生效的 LLM tools 名称列表并序列化为 JSON 字符串数组",
        category="capability",
    ),
    PromptVariableDefinition(
        placeholder="{{available_mcp_services}}",
        name="available_mcp_services",
        description="当前 Agent 绑定的 MCP 服务与MCP Tools可用工具列表 Markdown 树",
        value_logic="遍历 AgentDefinition.mcp_tools 查询绑定的 MCP 服务并格式化为层级树（未绑定则为空）",
        category="capability",
    ),
    PromptVariableDefinition(
        placeholder="{{available_subagents}}",
        name="available_subagents",
        description="当前 Agent 可创建的直接子 Agent 列表及功能说明",
        value_logic="遍历 AgentDefinition.available_sub_agent_ids 获取子 Agent 描述并格式化（未配置则为空）",
        category="capability",
    ),
    PromptVariableDefinition(
        placeholder="{{available_skills}}",
        name="available_skills",
        description="当前 Agent 声明的可用 Skills 列表及触发场景",
        value_logic="遍历 AgentDefinition.skill_ids 获取技能说明并格式化（未配置则为空）",
        category="capability",
    ),
    PromptVariableDefinition(
        placeholder="{{frontend_channel}}",
        name="frontend_channel",
        description="当前前端交互通道",
        value_logic="从当前请求的 render_options 中提取 channel/frontend_channel（如 web、cli，缺省为 unknown）",
        category="lifecycle",
    ),
    PromptVariableDefinition(
        placeholder="{{supported_text_formats}}",
        name="supported_text_formats",
        description="前端客户端支持渲染的排版文本格式",
        value_logic="从 render_options 中提取 supported_text_formats 并格式化为逗号分隔字符串",
        category="lifecycle",
    ),
    PromptVariableDefinition(
        placeholder="{{preferred_text_format}}",
        name="preferred_text_format",
        description="前端客户端首选推荐的排版格式",
        value_logic="从 render_options 中提取 preferred_text_format（缺省为 html）",
        category="lifecycle",
    ),
    PromptVariableDefinition(
        placeholder="{{NOTE_SNAPSHOT_JSON}}",
        name="NOTE_SNAPSHOT_JSON",
        description="Task Note 快照说明占位符",
        value_logic="渲染为固定文本：本次 user 消息中的 Task Note 快照",
        category="capability",
    ),
)

_VARIABLE_MAP: dict[str, PromptVariableDefinition] = {
    var.name: var for var in BUILTIN_PROMPT_VARIABLES
}


def get_builtin_prompt_variables() -> tuple[PromptVariableDefinition, ...]:
    """获取系统全部内置变量定义。"""
    return BUILTIN_PROMPT_VARIABLES


def list_builtin_prompt_variables() -> list[dict[str, str]]:
    """导出系统预置变量元数据字典列表（供 REST API 和界面查看）。"""
    return [var.to_dict() for var in BUILTIN_PROMPT_VARIABLES]


@dataclass(slots=True)
class PromptVariableContext:
    """提示词变量求值上下文，封装计算各变量所需的数据源。"""

    agent_instance_id: str
    agent_name: str
    current_working_directory: str
    parent_agent_instance_id: str = ""
    skill_root_directory: str = ""
    dedicated_skill_file: str = ""
    dedicated_memory_file: str = ""
    available_tools: tuple[str, ...] = ()
    available_mcp_services: str = ""
    available_subagents: str = ""
    available_skills: str = ""
    git_repo_roots: str = ""
    frontend_channel: str = "unknown"
    supported_text_formats: str = "html"
    preferred_text_format: str = "html"


def discover_git_roots_under(root: Path) -> list[str]:
    """在指定目录下递归发现 Git 仓库根目录。"""
    if not root.exists() or not root.is_dir():
        return []
    discovered: list[str] = []
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
    for current_root, dir_names, _file_names in os.walk(root):
        current_path = Path(current_root)
        if ".git" in dir_names:
            discovered.append(str(current_path))
            dir_names.remove(".git")
        dir_names[:] = [name for name in dir_names if name not in excluded]
    return sorted(discovered)


def build_prompt_variable_values(context: PromptVariableContext) -> dict[str, str]:
    """从上下文中提取全部 20 个内置变量的当前取值字典。"""
    resolved_cwd = Path(context.current_working_directory).expanduser().resolve(strict=False)
    git_roots_text = context.git_repo_roots
    if not git_roots_text and resolved_cwd.exists():
        roots = discover_git_roots_under(resolved_cwd)
        if roots:
            git_roots_text = "\n".join(f"- `{r}`" for r in roots)

    tools_json = json.dumps(list(context.available_tools), ensure_ascii=False)

    return {
        "agent_instance_id": context.agent_instance_id,
        "agent_name": context.agent_name,
        "parent_agent_instance_id": context.parent_agent_instance_id,
        "current_working_directory": str(resolved_cwd),
        "git_repo_roots": git_roots_text,
        "os_platform": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "system_architecture": platform.machine(),
        "python_version": platform.python_version(),
        "dedicated_skill_file": context.dedicated_skill_file,
        "dedicated_memory_file": context.dedicated_memory_file,
        "skill_root_directory": context.skill_root_directory,
        "available_tools": tools_json,
        "available_mcp_services": context.available_mcp_services,
        "available_subagents": context.available_subagents,
        "available_skills": context.available_skills,
        "frontend_channel": context.frontend_channel,
        "supported_text_formats": context.supported_text_formats,
        "preferred_text_format": context.preferred_text_format,
        "NOTE_SNAPSHOT_JSON": "本次 user 消息中的 Task Note 快照",
        "note_snapshot_json": "本次 user 消息中的 Task Note 快照",
    }


def render_prompt_variables(template_text: str, variables: Mapping[str, Any]) -> str:
    """对提示词文本中的 {{variable_name}} 占位符进行替换。

    如果遇到未知占位符（不在 variables 字典中），保持原样不报错。
    """
    if not template_text:
        return ""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in variables:
            val = variables[key]
            return "" if val is None else str(val)
        return match.group(0)

    return _VARIABLE_REGEX.sub(_replace, template_text)
