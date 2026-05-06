# -*- coding: utf-8 -*-
"""Agent 定义 CLI 接入适配。

提供 spread-ai agent 子命令组，支持 Agent 静态模板定义的增删改查和启用禁用。
复杂字段通过 YAML 文件输入，输出统一为 JSON，便于用户和外部 Agent 自动化调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import click
import yaml

from agent_runtime.agent_definition.agent_definition import (
    AgentDefinition,
    McpToolBinding,
    SystemPromptSegment,
)
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionCreateRequest,
    AgentDefinitionNorthGateway,
    AgentDefinitionUpdateRequest,
)


def _output_json(data: Any) -> None:
    """输出 JSON 到 stdout。"""
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _error_json(message: str, exit_code: int = 1) -> None:
    """输出失败响应 JSON 到 stderr 并退出。"""
    click.echo(
        json.dumps({"status": "error", "message": message}, ensure_ascii=False),
        err=True,
    )
    sys.exit(exit_code)


def _read_yaml_file(yaml_file: str) -> dict[str, Any]:
    """读取 YAML 文件并要求内容为对象结构。"""
    yaml_path = Path(yaml_file)
    if not yaml_path.exists():
        _error_json(f"文件不存在: {yaml_file}")
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        _error_json("YAML 文件内容必须是对象结构。")
    return data


def _definition_to_dict(definition: AgentDefinition) -> dict[str, Any]:
    """将 AgentDefinition 序列化为 CLI JSON 字典。"""
    return definition.to_dict()


def _create_request_from_yaml(data: dict[str, Any]) -> AgentDefinitionCreateRequest:
    """从完整 YAML 数据构建创建请求。"""
    agent_id = str(data.get("agent_id", ""))
    if not agent_id:
        raise ValueError("创建 Agent 定义时 YAML 必须包含 agent_id。")
    definition = AgentDefinition.from_dict(agent_id, data)
    return AgentDefinitionCreateRequest.from_definition(definition)


def _update_request_from_yaml(data: dict[str, Any]) -> AgentDefinitionUpdateRequest:
    """从局部 YAML 数据构建更新请求。

    YAML 中出现的字段会被更新，未出现的字段保持原值。
    """
    return AgentDefinitionUpdateRequest(
        name=data.get("name") if "name" in data else None,
        description=data.get("description") if "description" in data else None,
        group=str(data["group"]) if "group" in data else None,
        system_prompt=(
            _parse_system_prompt(data["system_prompt"])
            if "system_prompt" in data else None
        ),
        tool_ids=_parse_string_list(data["tool_ids"], "tool_ids")
        if "tool_ids" in data else None,
        mcp_tools=_parse_mcp_tools(data["mcp_tools"])
        if "mcp_tools" in data else None,
        skill_ids=_parse_string_list(data["skill_ids"], "skill_ids")
        if "skill_ids" in data else None,
        available_sub_agent_ids=_parse_string_list(
            data["available_sub_agent_ids"], "available_sub_agent_ids"
        ) if "available_sub_agent_ids" in data else None,
        default_llm_policy_group_id=data.get("default_llm_policy_group_id")
        if "default_llm_policy_group_id" in data else None,
        update_default_llm_policy_group_id="default_llm_policy_group_id" in data,
        allowed_llm_policy_group_ids=_parse_string_list(
            data["allowed_llm_policy_group_ids"], "allowed_llm_policy_group_ids"
        ) if "allowed_llm_policy_group_ids" in data else None,
        builtin_background_agent=bool(data["builtin_background_agent"])
        if "builtin_background_agent" in data else None,
        disabled=bool(data["disabled"]) if "disabled" in data else None,
    )


def _parse_system_prompt(value: Any) -> tuple[SystemPromptSegment, ...]:
    """解析 system_prompt YAML 数组。"""
    if not isinstance(value, list):
        raise ValueError("system_prompt 必须是数组。")
    segments: list[SystemPromptSegment] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("system_prompt 元素必须是对象结构。")
        segments.append(SystemPromptSegment.from_dict(item))
    return tuple(segments)


def _parse_mcp_tools(value: Any) -> tuple[McpToolBinding, ...]:
    """解析 mcp_tools YAML 数组。"""
    if not isinstance(value, list):
        raise ValueError("mcp_tools 必须是数组。")
    bindings: list[McpToolBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("mcp_tools 元素必须是对象结构。")
        bindings.append(McpToolBinding.from_dict(item))
    return tuple(bindings)


def _parse_string_list(value: Any, field_name: str) -> tuple[str, ...]:
    """解析字符串数组字段。"""
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组。")
    return tuple(str(item) for item in value)


def create_agent_definition_cli_group(
    gateway_factory: Callable[[], AgentDefinitionNorthGateway],
) -> click.Group:
    """创建 Agent 定义 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai agent 下。
    """

    @click.group("agent", help="Agent 静态模板定义管理")
    @click.pass_context
    def agent_group(ctx: click.Context) -> None:
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @agent_group.command("list", help="列出 Agent 定义")
    @click.option("--group", "group", default=None, help="按 Agent 分组筛选。")
    @click.option(
        "--exclude-disabled",
        is_flag=True,
        help="排除已禁用的 Agent 定义。",
    )
    @click.pass_context
    def list_definitions(
        ctx: click.Context,
        group: str | None,
        exclude_disabled: bool,
    ) -> None:
        """列出 Agent 定义。"""
        gateway = ctx.obj["gateway"]
        definitions = gateway.list_agent_definitions(
            group=group,
            include_disabled=not exclude_disabled,
        )
        _output_json([_definition_to_dict(item) for item in definitions])

    @agent_group.command("get", help="获取指定 Agent 定义详情")
    @click.argument("agent_id")
    @click.pass_context
    def get_definition(ctx: click.Context, agent_id: str) -> None:
        """查看指定 Agent 定义。"""
        gateway = ctx.obj["gateway"]
        try:
            definition = gateway.get_agent_definition(agent_id)
            _output_json(_definition_to_dict(definition))
        except KeyError as e:
            _error_json(str(e))

    @agent_group.command("create", help="从 YAML 文件创建 Agent 定义")
    @click.option("--file", "yaml_file", required=True, help="Agent 定义 YAML 文件路径。")
    @click.pass_context
    def create_definition(ctx: click.Context, yaml_file: str) -> None:
        """从 YAML 文件创建 Agent 定义。"""
        try:
            data = _read_yaml_file(yaml_file)
            gateway = ctx.obj["gateway"]
            request = _create_request_from_yaml(data)
            definition = gateway.create_agent_definition(request)
            _output_json({
                "status": "success",
                "message": f"已创建 Agent 定义: {definition.agent_id}",
                "agent_id": definition.agent_id,
            })
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"创建 Agent 定义失败: {e}")

    @agent_group.command("update", help="从 YAML 文件更新 Agent 定义")
    @click.argument("agent_id")
    @click.option("--file", "yaml_file", required=True, help="Agent 定义局部 YAML 文件路径。")
    @click.pass_context
    def update_definition(ctx: click.Context, agent_id: str, yaml_file: str) -> None:
        """从 YAML 文件局部更新 Agent 定义。"""
        try:
            data = _read_yaml_file(yaml_file)
            gateway = ctx.obj["gateway"]
            request = _update_request_from_yaml(data)
            definition = gateway.update_agent_definition(agent_id, request)
            _output_json({
                "status": "success",
                "message": f"已更新 Agent 定义: {definition.agent_id}",
                "agent_id": definition.agent_id,
            })
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"更新 Agent 定义失败: {e}")

    @agent_group.command("delete", help="删除 Agent 定义")
    @click.argument("agent_id")
    @click.pass_context
    def delete_definition(ctx: click.Context, agent_id: str) -> None:
        """删除指定 Agent 定义。"""
        gateway = ctx.obj["gateway"]
        gateway.delete_agent_definition(agent_id)
        _output_json({"status": "success", "message": f"已删除 Agent 定义: {agent_id}"})

    @agent_group.command("enable", help="启用 Agent 定义")
    @click.argument("agent_id")
    @click.pass_context
    def enable_definition(ctx: click.Context, agent_id: str) -> None:
        """启用指定 Agent 定义。"""
        gateway = ctx.obj["gateway"]
        try:
            definition = gateway.enable_agent_definition(agent_id)
            _output_json({
                "status": "success",
                "message": f"已启用 Agent 定义: {agent_id}",
                "disabled": definition.disabled,
            })
        except KeyError as e:
            _error_json(str(e))

    @agent_group.command("disable", help="禁用 Agent 定义")
    @click.argument("agent_id")
    @click.pass_context
    def disable_definition(ctx: click.Context, agent_id: str) -> None:
        """禁用指定 Agent 定义。"""
        gateway = ctx.obj["gateway"]
        try:
            definition = gateway.disable_agent_definition(agent_id)
            _output_json({
                "status": "success",
                "message": f"已禁用 Agent 定义: {agent_id}",
                "disabled": definition.disabled,
            })
        except KeyError as e:
            _error_json(str(e))

    return agent_group
