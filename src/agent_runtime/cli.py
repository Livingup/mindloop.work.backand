"""spread-ai 统一命令行入口（组合根）。

所有模块的 CLI 能力都挂在 spread-ai 命令下（如 spread-ai prompt ...、spread-ai tool ...）。
服务模式通过 spread-ai serve 启动 REST API 服务器。

组合根职责：
1. 提供 spread-ai config init，将用户提供的服务配置复制到 ~/.spread-ai/agent_runtime.yaml
2. 除 config init 外，所有命令固定加载 ~/.spread-ai/agent_runtime.yaml
3. 加载全局配置注册中心（ConfigRegistry）
4. 通过各模块工厂创建网关实例（延迟创建，命令执行时才触发）
5. 将网关工厂函数传递给 CLI adapter，由 adapter 在命令回调中调用
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import click

from agent_runtime.config import ConfigRegistry


def _runtime_config_path() -> Path:
    """返回初始化后的唯一运行配置文件路径。"""
    return Path.home() / ".spread-ai" / "agent_runtime.yaml"


def _load_initialized_runtime_config() -> None:
    """加载已经初始化好的运行配置；不存在时提示用户先执行 config init。"""
    config_path = _runtime_config_path()
    if not config_path.is_file():
        raise click.ClickException(
            f"运行配置文件不存在: {config_path}。请先执行 "
            "spread-ai config init --source <agent_runtime.yaml> 初始化服务配置。"
        )
    ConfigRegistry.load(str(config_path), watch=True)


def _echo_json(data: dict[str, object], *, err: bool = False) -> None:
    """输出结构化 JSON，方便用户、脚本和 Agent 解析。"""
    click.echo(json.dumps(data, ensure_ascii=False, indent=2), err=err)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """spread-ai：通用 Agent 运行时环境服务 CLI。"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit()
    if ctx.invoked_subcommand == "config":
        return
    _load_initialized_runtime_config()


# ── config 命令组 ────────────────────────────────────────────────


@click.group("config")
def config_group() -> None:
    """运行配置初始化与管理命令。"""


@config_group.command("init")
@click.option(
    "--source",
    "source_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="用于初始化的 agent_runtime.yaml 服务配置文件路径。",
)
@click.option(
    "--force",
    is_flag=True,
    help="目标配置已存在时允许覆盖。",
)
def init_config(source_path: Path, force: bool) -> None:
    """将用户提供的服务配置复制为 spread-ai 唯一运行配置。"""
    target_path = _runtime_config_path()
    target_exists_before_init = target_path.exists()
    if target_exists_before_init and not force:
        _echo_json(
            {
                "status": "error",
                "message": f"运行配置已存在: {target_path}。如需覆盖请增加 --force。",
                "config_path": str(target_path),
            },
            err=True,
        )
        raise click.exceptions.Exit(1)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    _echo_json(
        {
            "status": "ok",
            "message": "运行配置初始化完成。",
            "source_path": str(source_path),
            "config_path": str(target_path),
            "overwritten": target_exists_before_init,
        }
    )


main.add_command(config_group, name="config")


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
    import asyncio as _asyncio
    import logging as _logging
    from contextlib import asynccontextmanager, suppress

    import uvicorn
    from fastapi import FastAPI

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    _serve_logger = _logging.getLogger("agent_runtime.serve")

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
    from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory
    from agent_runtime.agent.north_gateway.agent_runtime_rest_adapter import (
        create_agent_runtime_router,
    )
    from agent_runtime.agent.agent_execution_factory import AgentExecutionFactory
    from agent_runtime.llm_model_registry.llm_model_registry_factory import (
        LlmModelRegistryFactory,
    )
    from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_rest_adapter import (
        create_llm_model_registry_router,
    )
    from agent_runtime.llm_http_trace.llm_http_trace_factory import (
        LlmHttpTraceFactory,
    )
    from agent_runtime.llm_http_trace.north_gateway.llm_http_trace_rest_adapter import (
        create_llm_http_trace_router,
    )
    from agent_runtime.system_settings.system_settings_factory import (
        SystemSettingsFactory,
    )
    from agent_runtime.system_settings.north_gateway.system_settings_rest_adapter import (
        create_system_settings_router,
    )
    from agent_runtime.file_tools.workspace_rest_adapter import create_workspace_router
    from agent_runtime.communication.communication_factory import CommunicationFactory
    from agent_runtime.chat_bubble.chat_bubble_factory import ChatBubbleFactory
    from agent_runtime.communication.north_gateway.communication_rest_adapter import (
        create_communication_router,
    )
    from agent_runtime.scene.scene_factory import SceneFactory
    from agent_runtime.scene.north_gateway.scene_rest_adapter import create_scene_router
    from agent_runtime.scene.scene_session_registry import SceneSessionRegistry
    from agent_runtime.terminal.terminal_factory import TerminalFactory
    from agent_runtime.terminal.north_gateway.terminal_rest_adapter import (
        create_terminal_router,
    )
    from agent_runtime.conversation_message.infrastructure import SqliteConversationRepository
    from agent_runtime.file_resource.north_gateway.file_resource_rest_adapter import (
        create_file_resource_router,
    )
    from agent_runtime.context_compression.north_gateway.context_compression_rest_adapter import (
        create_context_compression_router,
    )
    from agent_runtime.context_compression.prompt_repository import (
        ContextCompressionWorkerPromptRepository,
    )

    prompt_gateway = PromptTemplateFactory.create()
    tool_gateway = LocalToolFactory.create()
    skill_gateway = SkillFactory.create()
    mcp_gateway = McpClientFactory.create()
    # 注入 MCP 元工具依赖：5 个 mcp_* 本地元工具通过模块级单例持有 McpClientGateway，
    # 必须在 Agent 执行任何 mcp_get_service_detail/mcp_list_resources/... 之前完成注入，
    # 否则元工具会抛 "MCP 元工具未初始化"。注入与 REST 路由共用同一个 gateway 实例，
    # 共用同一个 McpSessionManager，复用 lifespan 已建立的连接池。
    from agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools import (
        configure as configure_mcp_meta_tools,
    )
    configure_mcp_meta_tools(mcp_gateway)
    agent_definition_gateway = AgentDefinitionFactory.create()
    communication_gateway = CommunicationFactory.create()
    chat_bubble_service = ChatBubbleFactory.create()
    conversation_repository = SqliteConversationRepository(
        ConfigRegistry.instance().conversation_repository.data_dir,
    )
    context_compression_prompt_repository = ContextCompressionWorkerPromptRepository(
        lambda: ConfigRegistry.instance().context_compression.archive_data_dir,
    )
    # scene_gateway 必须在 agent_runtime_gateway 之前创建，并注入给 AgentRuntimeFactory
    # 以支持 attach_scene 编排（前端进入 scene 的统一入口）。
    scene_gateway = SceneFactory.create()
    agent_runtime_gateway = AgentRuntimeFactory.create(
        scene_gateway=scene_gateway,
        communication_gateway=communication_gateway,
    )
    llm_model_registry_gateway = LlmModelRegistryFactory.create()
    llm_http_trace_service = LlmHttpTraceFactory.create()
    agent_execution_engine = AgentExecutionFactory.create(
        mcp_client_gateway=mcp_gateway,
    )
    terminal_gateway = TerminalFactory.create()
    system_settings_gateway = SystemSettingsFactory.create()

    # Scene 会话注册表：追踪前端连接状态，过期时自动 detach scene。
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import DetachSceneRequest
    from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING

    def _can_scene_expire(session) -> bool:
        """判断 scene 是否可以安全过期：scene 下没有 running 状态的 agent instance。"""
        try:
            tasks = agent_runtime_gateway.list_scene_tasks(session.scene_id)
            for task in tasks:
                if task.is_terminal:
                    continue
                for instance_id in task.agent_instance_ids:
                    try:
                        instance = agent_runtime_gateway.get_agent_instance(instance_id)
                        if instance.status == AGENT_INSTANCE_STATUS_RUNNING:
                            return False
                    except KeyError:
                        continue
            return True
        except Exception:
            # 查询异常时允许过期，避免永远无法关闭
            return True

    def _on_scene_session_expired(session) -> None:
        """SceneSessionRegistry 过期回调：取消 scene 下非终态 task/instances。"""
        try:
            agent_runtime_gateway.detach_scene(
                session.scene_id,
                DetachSceneRequest(workspace_path=session.workspace_path),
            )
        except Exception:
            _serve_logger.exception(
                "Scene 自动关闭失败: scene_id=%s", session.scene_id
            )

    scene_session_registry = SceneSessionRegistry(
        on_expired=_on_scene_session_expired,
        can_expire=_can_scene_expire,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """应用生命周期：启动时自动连接所有已启用的 MCP 服务，启动 Scene session sweep。"""
        async def auto_connect_mcp_services() -> None:
            _serve_logger.info("正在调度已启用 MCP 服务的后台连接…")
            try:
                results = await mcp_gateway.connect_all_enabled()
                _serve_logger.info("MCP 服务后台连接已调度：%d", len(results))
                for service_id, error in results.items():
                    if error is not None:
                        _serve_logger.warning("MCP 服务当前连接异常：%s — %s", service_id, error)
            except (Exception, _asyncio.CancelledError) as e:
                _serve_logger.exception("MCP 服务后台连接调度发生未预期错误，REST API 服务将继续启动：%s", e)

        scene_session_registry.start()
        auto_connect_task = _asyncio.create_task(auto_connect_mcp_services())
        try:
            yield
        finally:
            if not auto_connect_task.done():
                auto_connect_task.cancel()
                with suppress(Exception, _asyncio.CancelledError):
                    await auto_connect_task
            await mcp_gateway.shutdown()
            scene_session_registry.stop()

    app = FastAPI(title="spread-ai", description="通用 Agent 运行时环境服务", lifespan=lifespan)
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
        create_agent_runtime_router(
            agent_runtime_gateway,
            llm_model_registry_gateway,
            agent_execution_engine,
            agent_definition_gateway,
            scene_gateway=scene_gateway,
            session_registry=scene_session_registry,
            chat_bubble_service=chat_bubble_service,
            conversation_repository=conversation_repository,
        ),
        prefix="/api/agent-runtime",
        tags=["Agent 运行态"],
    )
    app.include_router(
        create_llm_model_registry_router(llm_model_registry_gateway),
        prefix="/api/llm-registry",
        tags=["LLM 服务商与模型注册"],
    )
    app.include_router(
        create_llm_http_trace_router(llm_http_trace_service),
        prefix="/api/llm-http-traces",
        tags=["LLM HTTP 请求日志"],
    )
    app.include_router(
        create_system_settings_router(system_settings_gateway),
        prefix="/api/system-settings",
        tags=["系统设置"],
    )
    app.include_router(
        create_workspace_router(),
        prefix="/api/workspaces",
        tags=["工作目录"],
    )
    app.include_router(
        create_communication_router(communication_gateway),
        prefix="/api/communication",
        tags=["通用通信"],
    )
    app.include_router(
        create_file_resource_router(),
        prefix="/api/file-resources",
        tags=["统一文件资源"],
    )
    app.include_router(
        create_context_compression_router(
            conversation_repository,
            prompt_repository=context_compression_prompt_repository,
        ),
        prefix="/api/context-compression",
        tags=["上下文压缩"],
    )
    # Scene REST router 本身已声明 prefix=/api/scene，这里只补充 OpenAPI tag。
    app.include_router(
        create_scene_router(scene_gateway),
        tags=["Scene 项目与场景"],
    )
    app.include_router(
        create_terminal_router(terminal_gateway),
        prefix="/api/scene",
        tags=["Scene Terminal"],
    )

    # host / port 优先用 CLI 参数，否则从配置文件读取。
    runtime_cfg = ConfigRegistry.instance().agent_runtime_service
    uvicorn.run(
        app,
        host=host or runtime_cfg.service_host,
        port=port or runtime_cfg.service_port,
    )
