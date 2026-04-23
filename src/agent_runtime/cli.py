"""spread-ai 统一命令行入口（组合根）。

所有模块的 CLI 能力都挂在 spread-ai 命令下（如 spread-ai prompt ...、spread-ai tool ...）。
服务模式通过 spread-ai serve 启动 REST API 服务器。

组合根职责：
1. 加载全局配置注册中心（ConfigRegistry）
2. 通过各模块工厂创建网关实例（延迟创建，命令执行时才触发）
3. 将网关工厂函数传递给 CLI adapter，由 adapter 在命令回调中调用
"""
from __future__ import annotations

import click

from agent_runtime.config import ConfigRegistry

# 默认配置文件路径（相对于项目根目录）。
_DEFAULT_CONFIG = "config/agent_runtime.yaml"


@click.group()
@click.option(
    "--config", "config_path",
    default=_DEFAULT_CONFIG,
    help="配置文件路径。",
    show_default=True,
)
def main(config_path: str) -> None:
    """spread-ai：通用 Agent 运行时环境服务 CLI。"""
    ConfigRegistry.load(config_path)


# ── @tool 装饰器注册（import 触发） ──────────────────────────────
import agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools  # noqa: F401  MCP 元工具


# ── 网关工厂函数（延迟创建，命令执行时 ConfigRegistry 已加载） ────


def _prompt_gateway_factory() -> object:
    """创建提示词模板北向网关实例。"""
    from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
    return PromptTemplateFactory.create(ConfigRegistry.instance().prompt_template)


def _tool_gateway_factory() -> object:
    """创建本地工具北向网关实例。"""
    from agent_runtime.tool.local_tool_factory import LocalToolFactory
    return LocalToolFactory.create()


def _skill_gateway_factory() -> object:
    """创建 Skill 北向网关实例。"""
    from agent_runtime.skill.skill_factory import SkillFactory
    return SkillFactory.create(ConfigRegistry.instance().skill_service)


def _mcp_gateway_factory() -> object:
    """创建 MCP 客户端北向网关实例。"""
    from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
    return McpClientFactory.create(ConfigRegistry.instance().mcp_client)


# ── 注册 CLI 子命令组 ──────────────────────────────────────────

from agent_runtime.prompt_template.north_gateway.prompt_snippet_cli_adapter import (
    create_prompt_cli_group,
)
from agent_runtime.tool.north_gateway.local_tool_cli_adapter import (
    create_tool_cli_group,
)
from agent_runtime.skill.north_gateway.skill_cli_adapter import create_skill_cli_group
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_cli_adapter import (
    create_mcp_cli_group,
    create_mcp_config_cli_group,
)

main.add_command(create_prompt_cli_group(_prompt_gateway_factory), name="prompt")
main.add_command(create_tool_cli_group(_tool_gateway_factory), name="tool")
main.add_command(create_skill_cli_group(_skill_gateway_factory), name="skill")
main.add_command(create_mcp_config_cli_group(_mcp_gateway_factory), name="mcp-config")
main.add_command(create_mcp_cli_group(_mcp_gateway_factory), name="mcp")


# ── serve 命令 ─────────────────────────────────────────────────


@main.command("serve")
@click.option("--host", default=None, help="监听地址（覆盖配置文件）。")
@click.option("--port", default=None, type=int, help="监听端口（覆盖配置文件）。")
def serve(host: str | None, port: int | None) -> None:
    """启动 REST API 服务器。"""
    import uvicorn
    from fastapi import FastAPI

    cfg = ConfigRegistry.instance()

    # 各模块网关统一从 ConfigRegistry 获取配置创建。
    from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
    from agent_runtime.prompt_template.north_gateway.prompt_snippet_rest_adapter import (
        create_prompt_snippet_router,
    )
    from agent_runtime.tool.local_tool_factory import LocalToolFactory
    from agent_runtime.tool.north_gateway.local_tool_rest_adapter import (
        create_local_tool_router,
    )
    from agent_runtime.skill.skill_factory import SkillFactory
    from agent_runtime.skill.north_gateway.skill_rest_adapter import create_skill_router
    from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
    from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_rest_adapter import (
        create_mcp_config_router,
        create_mcp_router,
    )

    prompt_gateway = PromptTemplateFactory.create(cfg.prompt_template)
    tool_gateway = LocalToolFactory.create()
    skill_gateway = SkillFactory.create(cfg.skill_service)
    mcp_gateway = McpClientFactory.create(cfg.mcp_client)

    app = FastAPI(title="spread-ai", description="通用 Agent 运行时环境服务")
    app.include_router(
        create_prompt_snippet_router(prompt_gateway),
        prefix="/api/prompt-snippets",
        tags=["提示词片段"],
    )
    app.include_router(
        create_local_tool_router(tool_gateway),
        prefix="/api/tools",
        tags=["本地工具"],
    )
    app.include_router(
        create_mcp_config_router(mcp_gateway),
        prefix="/api/mcp-config",
        tags=["MCP 服务配置管理"],
    )
    app.include_router(
        create_mcp_router(mcp_gateway),
        prefix="/api/mcp",
        tags=["MCP 服务使用"],
    )
    app.include_router(
        create_skill_router(skill_gateway),
        prefix="/api/skills",
        tags=["Skill"],
    )

    # host / port 优先用 CLI 参数，否则从配置文件读取。
    runtime_cfg = cfg.agent_runtime_service
    uvicorn.run(
        app,
        host=host or runtime_cfg.service_host,
        port=port or runtime_cfg.service_port,
    )
