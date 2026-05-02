# -*- coding: utf-8 -*-
"""MCP 客户端 CLI 接入适配。

拆分为两个独立命令组：
- spread-ai mcp-config：MCP 服务配置管理（管理后台，纯本地 CRUD）
- spread-ai mcp：MCP 服务使用（C 端消费，需连接远程服务）

输出格式为 JSON，方便 Agent / 外部 AI 解析。
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Callable

import click

from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import (
    McpClientGateway,
    McpServiceAddRequest,
    McpServiceUpdateRequest,
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


def _run_async(coro: Any, gateway: Any = None) -> Any:
    """在同步 CLI 环境中运行异步协程。

    CLI 是一次性命令，执行完后需主动断开 MCP 连接，
    否则事件循环关闭时异步生成器清理会报错。
    """

    async def _run_and_cleanup() -> Any:
        try:
            return await coro
        finally:
            if gateway is not None:
                await gateway.shutdown()

    return asyncio.run(_run_and_cleanup())


# ══════════════════════════════════════════════════════════════════
# 配置管理命令组：spread-ai mcp-config
# 管理后台，纯本地 CRUD，不联网
# ══════════════════════════════════════════════════════════════════


def create_mcp_config_cli_group(gateway_factory: Callable[[], McpClientGateway]) -> click.Group:
    """创建 MCP 服务配置管理 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai mcp-config 下。
    """

    @click.group("mcp-config", help="MCP 服务配置管理（本地 CRUD）")
    @click.pass_context
    def mcp_config_group(ctx: click.Context) -> None:
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @mcp_config_group.command("list", help="列出所有已配置的 MCP 服务")
    @click.pass_context
    def list_services(ctx: click.Context) -> None:
        """列出所有 MCP 服务配置。"""
        gateway = ctx.obj["gateway"]
        try:
            services = gateway.list_services()
            items = [
                {
                    "service_id": s.service_id,
                    "name": s.name,
                    "description": s.description,
                    "transport_type": s.transport_type,
                    "enabled": s.enabled,
                    "group": s.group,
                }
                for s in services
            ]
            _output_json(items)
        except Exception as e:
            _error_json(str(e))

    @mcp_config_group.command("get", help="获取指定 MCP 服务的定义配置")
    @click.argument("service_id")
    @click.pass_context
    def get_service(ctx: click.Context, service_id: str) -> None:
        """查看指定 MCP 服务的配置详情。"""
        gateway = ctx.obj["gateway"]
        try:
            info = gateway.get_service(service_id)
            data: dict[str, Any] = {
                "service_id": info.service_id,
                "name": info.name,
                "description": info.description,
                "transport": info.transport,
                "enabled": info.enabled,
                "group": info.group,
            }
            if info.usage_guide:
                data["usage_guide"] = info.usage_guide
            _output_json(data)
        except KeyError as e:
            _error_json(str(e))

    @mcp_config_group.command("add", help="从 YAML 文件添加 MCP 服务配置")
    @click.option("--file", "yaml_file", required=True, help="YAML 配置文件路径")
    @click.option("--id", "service_id", required=True, help="服务 ID")
    @click.pass_context
    def add_service(ctx: click.Context, yaml_file: str, service_id: str) -> None:
        """从 YAML 文件添加 MCP 服务配置。"""
        import yaml
        from pathlib import Path

        try:
            yaml_path = Path(yaml_file)
            if not yaml_path.exists():
                _error_json(f"文件不存在: {yaml_file}")

            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not isinstance(data, dict):
                _error_json("YAML 文件内容必须是字典格式")

            gateway = ctx.obj["gateway"]
            config = McpServiceAddRequest(
                service_id=service_id,
                name=data.get("name", ""),
                transport=data.get("transport", {"type": "stdio"}),
                description=data.get("description"),
                enabled=data.get("enabled", True),
                group=data.get("group", "default"),
                usage_guide=data.get("usage_guide"),
            )
            gateway.add_service(config)
            _output_json({"status": "success", "message": f"已添加 MCP 服务: {service_id}"})
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"添加服务失败: {e}")

    @mcp_config_group.command("remove", help="删除 MCP 服务配置")
    @click.argument("service_id")
    @click.pass_context
    def remove_service(ctx: click.Context, service_id: str) -> None:
        """删除指定 MCP 服务的配置并断开连接。"""
        gateway = ctx.obj["gateway"]
        try:
            _run_async(gateway.remove_service(service_id), gateway)
            _output_json({"status": "success", "message": f"已删除 MCP 服务: {service_id}"})
        except KeyError as e:
            _error_json(str(e))

    @mcp_config_group.command("update", help="从 YAML 文件更新 MCP 服务配置")
    @click.argument("service_id")
    @click.option("--file", "yaml_file", required=True, help="YAML 配置文件路径")
    @click.pass_context
    def update_service(ctx: click.Context, service_id: str, yaml_file: str) -> None:
        """从 YAML 文件更新指定 MCP 服务的配置（部分更新）。"""
        import yaml
        from pathlib import Path

        try:
            yaml_path = Path(yaml_file)
            if not yaml_path.exists():
                _error_json(f"文件不存在: {yaml_file}")

            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not isinstance(data, dict):
                _error_json("YAML 文件内容必须是字典格式")

            gateway = ctx.obj["gateway"]
            update = McpServiceUpdateRequest(
                name=data.get("name"),
                description=data.get("description"),
                transport=data.get("transport"),
                group=data.get("group"),
                usage_guide=data.get("usage_guide"),
            )
            gateway.update_service(service_id, update)
            _output_json({"status": "success", "message": f"已更新 MCP 服务: {service_id}"})
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"更新服务失败: {e}")

    @mcp_config_group.command("enable", help="启用 MCP 服务")
    @click.argument("service_id")
    @click.pass_context
    def enable_service(ctx: click.Context, service_id: str) -> None:
        """启用指定 MCP 服务。"""
        gateway = ctx.obj["gateway"]
        try:
            gateway.enable_service(service_id)
            _output_json({"status": "success", "message": f"已启用 MCP 服务: {service_id}"})
        except KeyError as e:
            _error_json(str(e))

    @mcp_config_group.command("disable", help="禁用 MCP 服务")
    @click.argument("service_id")
    @click.pass_context
    def disable_service(ctx: click.Context, service_id: str) -> None:
        """禁用指定 MCP 服务。"""
        gateway = ctx.obj["gateway"]
        try:
            gateway.disable_service(service_id)
            _output_json({"status": "success", "message": f"已禁用 MCP 服务: {service_id}"})
        except KeyError as e:
            _error_json(str(e))

    return mcp_config_group


# ══════════════════════════════════════════════════════════════════
# MCP 服务使用命令组：spread-ai mcp
# C 端消费，需连接远程 MCP 服务
# ══════════════════════════════════════════════════════════════════


def create_mcp_cli_group(gateway_factory: Callable[[], McpClientGateway]) -> click.Group:
    """创建 MCP 服务使用 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai mcp 下。
    """

    @click.group("mcp", help="MCP 服务使用（需连接远程服务）")
    @click.pass_context
    def mcp_group(ctx: click.Context) -> None:
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @mcp_group.command("inspect", help="查看 MCP 服务的能力和使用指南")
    @click.argument("service_id")
    @click.pass_context
    def inspect_service(ctx: click.Context, service_id: str) -> None:
        """连接远程服务，获取 capabilities + instructions。"""
        gateway = ctx.obj["gateway"]
        try:
            detail = _run_async(gateway.get_service_detail(service_id), gateway)
            _output_json({
                "supports_tools": detail.supports_tools,
                "supports_resources": detail.supports_resources,
                "server_name": detail.server_name,
                "server_version": detail.server_version,
                "instructions": detail.instructions,
            })
        except (KeyError, ConnectionError, ValueError) as e:
            _error_json(str(e))

    @mcp_group.command("tools", help="列出 MCP 服务的工具")
    @click.argument("service_id")
    @click.pass_context
    def list_tools(ctx: click.Context, service_id: str) -> None:
        """列出指定 MCP 服务的所有工具。"""
        gateway = ctx.obj["gateway"]
        try:
            tools = _run_async(gateway.list_tools(service_id), gateway)
            items = [
                {"name": t.name, "description": t.description} for t in tools
            ]
            _output_json(items)
        except (KeyError, ConnectionError, ValueError) as e:
            _error_json(str(e))

    @mcp_group.command("tool-detail", help="查看工具参数定义")
    @click.argument("service_id")
    @click.argument("tool_names", nargs=-1, required=True)
    @click.pass_context
    def tool_detail(ctx: click.Context, service_id: str, tool_names: tuple[str, ...]) -> None:
        """查看指定工具的 inputSchema。"""
        gateway = ctx.obj["gateway"]
        try:
            tools = _run_async(
                gateway.get_tool_detail(service_id, list(tool_names)), gateway
            )
            items = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
            _output_json(items)
        except (KeyError, ConnectionError, ValueError) as e:
            _error_json(str(e))

    @mcp_group.command("resources", help="列出 MCP 服务的资源")
    @click.argument("service_id")
    @click.pass_context
    def list_resources(ctx: click.Context, service_id: str) -> None:
        """列出指定 MCP 服务的所有资源。"""
        gateway = ctx.obj["gateway"]
        try:
            resources = _run_async(gateway.list_resources(service_id), gateway)
            items = [
                {
                    "uri": r.uri,
                    "name": r.name,
                    "description": r.description,
                    "mime_type": r.mime_type,
                }
                for r in resources
            ]
            _output_json(items)
        except (KeyError, ConnectionError, ValueError) as e:
            _error_json(str(e))

    @mcp_group.command("read-resource", help="读取 MCP 服务的资源内容")
    @click.argument("service_id")
    @click.argument("uri")
    @click.pass_context
    def read_resource(ctx: click.Context, service_id: str, uri: str) -> None:
        """读取指定 MCP 服务的资源内容。"""
        gateway = ctx.obj["gateway"]
        try:
            content = _run_async(gateway.read_resource(service_id, uri), gateway)
            data: dict[str, Any] = {
                "uri": content.uri,
                "mime_type": content.mime_type,
            }
            if content.text is not None:
                data["text"] = content.text
            if content.blob is not None:
                data["blob"] = content.blob
            _output_json(data)
        except (KeyError, ConnectionError, ValueError) as e:
            _error_json(str(e))

    return mcp_group
