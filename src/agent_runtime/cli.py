"""spread-ai 统一命令行入口。

所有模块的 CLI 能力都挂在 spread-ai 命令下（如 spread-ai prompt ...、spread-ai tool ...）。
服务模式通过 spread-ai serve 启动 REST API 服务器。
"""
from __future__ import annotations

from pathlib import Path

import click

from agent_runtime.tool.local_tool_factory import LocalToolFactory
from agent_runtime.tool.north_gateway.local_tool_cli_adapter import (
    create_tool_cli_group,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_cli_adapter import (
    prompt_group,
)


@click.group()
def main() -> None:
    """spread-ai：通用 Agent 运行时环境服务 CLI。"""
    pass


# 注册提示词模板模块的子命令组。
main.add_command(prompt_group, name="prompt")

# 导入工具模块，触发 @tool 装饰器自动注册到全局仓储。
import agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools  # noqa: F401  MCP 元工具

# 注册本地工具模块的子命令组。
_tool_gateway = LocalToolFactory.create()
main.add_command(create_tool_cli_group(_tool_gateway), name="tool")

# 注册 MCP 客户端模块的两个子命令组。
from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_cli_adapter import (
    create_mcp_cli_group,
    create_mcp_config_cli_group,
)

_mcp_gateway = McpClientFactory.create()
main.add_command(create_mcp_config_cli_group(_mcp_gateway), name="mcp-config")
main.add_command(create_mcp_cli_group(_mcp_gateway), name="mcp")


@main.command("serve")
@click.option("--host", default="127.0.0.1", help="监听地址。", show_default=True)
@click.option("--port", default=8000, type=int, help="监听端口。", show_default=True)
@click.option(
    "--snippets-dir",
    default=str(Path.home() / ".spread-ai" / "prompt-snippets"),
    help="提示词片段存储目录路径。",
    show_default=True,
)
def serve(host: str, port: int, snippets_dir: str) -> None:
    """启动 REST API 服务器。"""
    import uvicorn
    from fastapi import FastAPI

    from agent_runtime.tool.north_gateway.local_tool_rest_adapter import (
        create_local_tool_router,
    )
    from agent_runtime.prompt_template.north_gateway.prompt_snippet_rest_adapter import (
        create_prompt_snippet_router,
    )
    from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
    from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory

    # 提示词模板模块。
    config = PromptTemplateConfig(snippets_dir=snippets_dir)
    prompt_gateway = PromptTemplateFactory.create(config)

    # 本地工具模块。
    tool_gateway = LocalToolFactory.create()

    # MCP 客户端模块。
    from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_rest_adapter import (
        create_mcp_config_router,
        create_mcp_router,
    )

    mcp_gateway = McpClientFactory.create()

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

    uvicorn.run(app, host=host, port=port)
