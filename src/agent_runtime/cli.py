"""spread-ai 统一命令行入口（组合根）。

所有模块的 CLI 能力都挂在 spread-ai 命令下（如 spread-ai prompt ...、spread-ai tool ...）。
服务模式通过 spread-ai serve 启动 REST API 服务器。

组合根职责：
1. 解析 CLI 配置文件路径（--config → SPREAD_AI_CONFIG → ~/.spread-ai/agent_runtime.yaml）
2. 首次运行自动生成用户默认配置
3. 加载全局配置注册中心（ConfigRegistry）
4. 通过各模块工厂创建网关实例（延迟创建，命令执行时才触发）
5. 将网关工厂函数传递给 CLI adapter，由 adapter 在命令回调中调用
"""
from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import click

from agent_runtime.config import ConfigRegistry

# 配置环境变量名称。
_CONFIG_ENV_NAME = "SPREAD_AI_CONFIG"


def _user_default_config_path() -> Path:
    """返回发布态默认用户配置文件路径。"""
    return Path.home() / ".spread-ai" / "agent_runtime.yaml"


def _ensure_user_default_config() -> str:
    """确保用户默认配置存在；不存在时从包内模板首次生成。"""
    user_config_path = _user_default_config_path()
    if user_config_path.exists():
        return str(user_config_path)

    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    template_path = resources.files("agent_runtime.defaults").joinpath("agent_runtime.yaml")
    user_config_path.write_text(
        template_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return str(user_config_path)


def _resolve_config_path(config_path: str | None) -> str:
    """按优先级解析 CLI 配置文件路径。

    优先级：
    1. 用户显式传入 --config
    2. 环境变量 SPREAD_AI_CONFIG
    3. 用户目录 ~/.spread-ai/agent_runtime.yaml
    4. 若用户目录配置不存在，则首次运行自动生成默认配置
    """
    if config_path is not None:
        return str(Path(os.path.expanduser(config_path)))

    env_config_path = os.environ.get(_CONFIG_ENV_NAME)
    if env_config_path:
        return str(Path(os.path.expanduser(env_config_path)))

    return _ensure_user_default_config()


@click.group()
@click.option(
    "--config", "config_path",
    default=None,
    help="配置文件路径（优先级高于环境变量 SPREAD_AI_CONFIG 和用户默认配置）。",
)
def main(config_path: str | None) -> None:
    """spread-ai：通用 Agent 运行时环境服务 CLI。"""
    ConfigRegistry.load(_resolve_config_path(config_path))


# ── @tool 装饰器注册（import 触发） ──────────────────────────────
import agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools  # noqa: F401  MCP 元工具


# ── 网关工厂函数（延迟创建，命令执行时 ConfigRegistry 已加载） ────


def _prompt_gateway_factory() -> object:
    """创建提示词模板北向网关实例。"""
    from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
    return PromptTemplateFactory.create()


def _tool_gateway_factory() -> object:
    """创建本地工具北向网关实例。"""
    from agent_runtime.tool.local_tool_factory import LocalToolFactory
    return LocalToolFactory.create()


def _skill_gateway_factory() -> object:
    """创建 Skill 北向网关实例。"""
    from agent_runtime.skill.skill_factory import SkillFactory
    return SkillFactory.create()


def _mcp_gateway_factory() -> object:
    """创建 MCP 客户端北向网关实例。"""
    from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
    return McpClientFactory.create()


def _agent_definition_gateway_factory() -> object:
    """创建 Agent 定义北向网关实例。"""
    from agent_runtime.agent_definition.agent_definition_factory import (
        AgentDefinitionFactory,
    )
    return AgentDefinitionFactory.create()


def _llm_model_registry_gateway_factory() -> object:
    """创建 LLM 服务商与模型注册北向网关实例。"""
    from agent_runtime.llm_model_registry.llm_model_registry_factory import (
        LlmModelRegistryFactory,
    )
    return LlmModelRegistryFactory.create()


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
from agent_runtime.agent_definition.north_gateway.agent_definition_cli_adapter import (
    create_agent_definition_cli_group,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_cli_adapter import (
    create_llm_model_registry_cli_group,
)

main.add_command(create_prompt_cli_group(_prompt_gateway_factory), name="prompt")
main.add_command(create_tool_cli_group(_tool_gateway_factory), name="tool")
main.add_command(create_skill_cli_group(_skill_gateway_factory), name="skill")
main.add_command(create_mcp_config_cli_group(_mcp_gateway_factory), name="mcp-config")
main.add_command(create_mcp_cli_group(_mcp_gateway_factory), name="mcp")
main.add_command(
    create_agent_definition_cli_group(_agent_definition_gateway_factory),
    name="agent",
)
main.add_command(
    create_llm_model_registry_cli_group(_llm_model_registry_gateway_factory),
    name="llm-registry",
)


# ── serve 命令 ─────────────────────────────────────────────────


@main.command("serve")
@click.option("--host", default=None, help="监听地址（覆盖配置文件）。")
@click.option("--port", default=None, type=int, help="监听端口（覆盖配置文件）。")
def serve(host: str | None, port: int | None) -> None:
    """启动 REST API 服务器。"""
    import uvicorn
    from fastapi import FastAPI

    # 各模块网关通过无参工厂创建，配置由工厂内部从 ConfigRegistry 实时读取。
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
    from agent_runtime.agent_definition.agent_definition_factory import (
        AgentDefinitionFactory,
    )
    from agent_runtime.agent_definition.north_gateway.agent_definition_rest_adapter import (
        create_agent_definition_router,
    )
    from agent_runtime.llm_model_registry.llm_model_registry_factory import (
        LlmModelRegistryFactory,
    )
    from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_rest_adapter import (
        create_llm_model_registry_router,
    )

    prompt_gateway = PromptTemplateFactory.create()
    tool_gateway = LocalToolFactory.create()
    skill_gateway = SkillFactory.create()
    mcp_gateway = McpClientFactory.create()
    agent_definition_gateway = AgentDefinitionFactory.create()
    llm_model_registry_gateway = LlmModelRegistryFactory.create()

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
    app.include_router(
        create_agent_definition_router(agent_definition_gateway),
        prefix="/api/agents",
        tags=["Agent 定义"],
    )
    app.include_router(
        create_llm_model_registry_router(llm_model_registry_gateway),
        prefix="/api/llm-registry",
        tags=["LLM 服务商与模型注册"],
    )

    # host / port 优先用 CLI 参数，否则从配置文件读取。
    runtime_cfg = ConfigRegistry.instance().agent_runtime_service
    uvicorn.run(
        app,
        host=host or runtime_cfg.service_host,
        port=port or runtime_cfg.service_port,
    )
