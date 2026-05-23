from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agent_runtime.config import ConfigRegistry

logger = logging.getLogger("agent_runtime.serve")


def load_runtime_config(
    config_file_path: str | Path,
    env_file_path: str | Path | None = None,
    *,
    watch: bool = True,
) -> None:
    ConfigRegistry.load(
        str(Path(config_file_path).resolve()),
        str(Path(env_file_path).resolve()) if env_file_path is not None else None,
        watch=watch,
    )


def create_app() -> FastAPI:
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
    from agent_runtime.context_compression.settings_repository import (
        ContextCompressionSettingsRepository,
    )
    from agent_runtime.lacp.lacp_factory import LacpFactory
    from agent_runtime.lacp.north_gateway.lacp_rest_adapter import create_lacp_router

    prompt_gateway = PromptTemplateFactory.create()
    tool_gateway = LocalToolFactory.create()
    skill_gateway = SkillFactory.create()
    mcp_gateway = McpClientFactory.create()
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
    context_compression_settings_repository = ContextCompressionSettingsRepository(
        lambda: ConfigRegistry.instance().context_compression.archive_data_dir,
    )
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
    lacp_bridge = LacpFactory.create_bridge(
        runtime_gateway=agent_runtime_gateway,
        agent_definition_gateway=agent_definition_gateway,
        execution_engine=agent_execution_engine,
    )
    lacp_client = LacpFactory.create_client()
    lacp_settings_repository = LacpFactory.create_settings_repository()
    terminal_gateway = TerminalFactory.create()
    system_settings_gateway = SystemSettingsFactory.create()

    from agent_runtime.agent.north_gateway.agent_runtime_contracts import DetachSceneRequest
    from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING

    def _can_scene_expire(session) -> bool:
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
            return True

    def _on_scene_session_expired(session) -> None:
        try:
            agent_runtime_gateway.detach_scene(
                session.scene_id,
                DetachSceneRequest(workspace_path=session.workspace_path),
            )
        except Exception:
            logger.exception(
                "Scene 自动关闭失败: scene_id=%s",
                session.scene_id,
            )

    scene_session_registry = SceneSessionRegistry(
        on_expired=_on_scene_session_expired,
        can_expire=_can_scene_expire,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async def auto_connect_mcp_services() -> None:
            logger.info("正在调度已启用 MCP 服务的后台连接…")
            try:
                results = await mcp_gateway.connect_all_enabled()
                logger.info("MCP 服务后台连接已调度：%d", len(results))
                for service_id, error in results.items():
                    if error is not None:
                        logger.warning("MCP 服务当前连接异常：%s — %s", service_id, error)
            except (Exception, asyncio.CancelledError) as e:
                logger.exception("MCP 服务后台连接调度发生未预期错误，REST API 服务将继续启动：%s", e)

        scene_session_registry.start()
        auto_connect_task = asyncio.create_task(auto_connect_mcp_services())
        lacp_start_task = asyncio.create_task(lacp_client.start())
        try:
            yield
        finally:
            if not auto_connect_task.done():
                auto_connect_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await auto_connect_task
            if not lacp_start_task.done():
                lacp_start_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await lacp_start_task
            await lacp_client.shutdown()
            await mcp_gateway.shutdown()
            scene_session_registry.stop()

    app = FastAPI(title="spread-ai", description="通用 Agent 运行时环境服务", lifespan=lifespan)

    @app.get("/api/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

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
        create_lacp_router(lacp_bridge, lacp_settings_repository),
        prefix="/api/lacp",
        tags=["LACP"],
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
            settings_repository=context_compression_settings_repository,
            llm_registry_gateway=llm_model_registry_gateway,
            config_accessor=lambda: ConfigRegistry.instance().context_compression,
        ),
        prefix="/api/context-compression",
        tags=["上下文压缩"],
    )
    app.include_router(
        create_scene_router(scene_gateway),
        tags=["Scene 项目与场景"],
    )
    app.include_router(
        create_terminal_router(terminal_gateway),
        prefix="/api/scene",
        tags=["Scene Terminal"],
    )
    return app


def run_server(
    *,
    config_file_path: str | Path,
    env_file_path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    watch: bool = True,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    load_runtime_config(config_file_path, env_file_path, watch=watch)
    app = create_app()
    runtime_cfg = ConfigRegistry.instance().agent_runtime_service
    uvicorn.run(
        app,
        host=host or runtime_cfg.service_host,
        port=port or runtime_cfg.service_port,
    )
