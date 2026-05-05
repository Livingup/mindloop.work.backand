"""本地工具 CLI 接入适配。

提供 spread-ai tool 子命令组，供 Agent / 外部 AI 发现和查询系统工具。
输出格式为 JSON，方便 Agent / 外部 AI 解析。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

import click

from agent_runtime.llm_api_protocol import SDK_PLATFORM_ANTHROPIC
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
)


def _success(message: str) -> None:
    """输出成功响应。"""
    click.echo(json.dumps(
        {"status": "success", "message": message},
        ensure_ascii=False,
    ))


def _error_json(message: str, exit_code: int = 1) -> None:
    """输出失败响应 JSON 到 stderr 并退出。"""
    click.echo(
        json.dumps({"status": "error", "message": message}, ensure_ascii=False),
        err=True,
    )
    sys.exit(exit_code)


def _serialize_summary(item: Any) -> dict[str, Any]:
    """将 ToolSummary 序列化为字典。"""
    return {
        "tool_name": item.tool_name,
        "description": item.description,
        "group": item.group,
        "wiki": item.wiki,
        "category": item.category,
        "always_enabled": item.always_enabled,
        "allowed_loop_decisions": item.allowed_loop_decisions,
    }


def _serialize_detail(item: Any) -> dict[str, Any]:
    """将 ToolDetail 序列化为字典。"""
    return {
        "tool_name": item.tool_name,
        "description": item.description,
        "group": item.group,
        "public": item.public,
        "wiki": item.wiki,
        "category": item.category,
        "always_enabled": item.always_enabled,
        "allowed_loop_decisions": item.allowed_loop_decisions,
        "schema": item.schemas.get(SDK_PLATFORM_ANTHROPIC, {}),
    }




def create_tool_cli_group(gateway_factory: Callable[[], LocalToolGateway]) -> click.Group:
    """创建本地工具 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂在 spread-ai tool 下。
    """

    @click.group("tool")
    @click.pass_context
    def tool_group(ctx: click.Context) -> None:
        """本地工具管理：查询和列出系统工具。"""
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @tool_group.command("list")
    @click.option(
        "--group", "filter_group",
        default=None,
        help="按指定分组筛选工具。",
    )
    @click.option(
        "--group-by",
        is_flag=True,
        default=False,
        help="按 group 分组返回结果。",
    )
    @click.pass_context
    def list_tools(ctx: click.Context, filter_group: str | None, group_by: bool) -> None:
        """列出所有公开工具。"""
        gateway = ctx.obj["gateway"]
        result = gateway.list_tools(public_only=True, group_by=group_by)

        if group_by:
            # result 是 dict[str, list[ToolSummary]]
            output: dict[str, Any] = {}
            for group_name, items in result.items():
                if filter_group and group_name != filter_group:
                    continue
                output[group_name] = [_serialize_summary(i) for i in items]
            click.echo(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            # result 是 list[ToolSummary]
            items = result
            if filter_group:
                items = [i for i in items if i.group == filter_group]
            output_list = [_serialize_summary(i) for i in items]
            click.echo(json.dumps(output_list, ensure_ascii=False, indent=2))

    @tool_group.command("get")
    @click.argument("names")
    @click.pass_context
    def get_tool(ctx: click.Context, names: str) -> None:
        """查询指定工具详情。NAMES 为逗号分隔的工具名列表（如 add,greet）。"""
        gateway = ctx.obj["gateway"]
        name_list = [n.strip() for n in names.split(",") if n.strip()]
        if not name_list:
            _error_json("请提供至少一个工具名称。")
            return
        results: list[dict[str, Any]] = []
        for n in name_list:
            try:
                detail = gateway.get_tool(n)
            except KeyError as e:
                _error_json(str(e))
                return
            results.append(_serialize_detail(detail))
        if len(results) == 1:
            click.echo(json.dumps(results[0], ensure_ascii=False, indent=2))
        else:
            click.echo(json.dumps(results, ensure_ascii=False, indent=2))

    @tool_group.command("groups")
    @click.pass_context
    def list_groups(ctx: click.Context) -> None:
        """列出所有工具分组名称。"""
        gateway = ctx.obj["gateway"]
        grouped = gateway.list_tools(public_only=True, group_by=True)
        click.echo(json.dumps(sorted(grouped.keys()), ensure_ascii=False, indent=2))

    return tool_group
