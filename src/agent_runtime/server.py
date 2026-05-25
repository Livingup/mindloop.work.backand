from __future__ import annotations

import asyncio
import logging
import time
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
    from agent_runtime.llm_model_registry.llm_execution_policy import (
        UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE,
    )
    from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_IMAGES_GENERATIONS
    from agent_runtime.llm_http_trace.llm_http_trace_factory import (
        LlmHttpTraceFactory,
    )
    from agent_runtime.llm_http_trace.north_gateway.llm_http_trace_rest_adapter import (
        create_llm_http_trace_router,
    )
    from agent_runtime.image_generation.north_gateway.image_generation_rest_adapter import (
        create_image_generation_router,
    )
    from agent_runtime.image_generation.service import ImageGenerationService
    from agent_runtime.image_generation.settings_repository import ImageGenerationSettingsRepository
    from agent_runtime.file_resource.shared_service import get_file_resource_service
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
    from agent_runtime.codex_oauth.factory import CodexOAuthFactory
    from agent_runtime.codex_oauth.north_gateway.codex_oauth_rest_adapter import (
        create_codex_oauth_router,
    )
    from agent_runtime.codex_oauth.north_gateway.local_callback_server import (
        CodexOAuthLocalCallbackServer,
    )
    from agent_runtime.runtime_health import EventLoopLagMonitor
    from agent_runtime.runtime_network_monitor import (
        new_network_trace_id,
        runtime_network_monitor,
    )

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
    image_generation_registry_gateway = LlmModelRegistryFactory.create_image_generation_registry()
    image_generation_service = ImageGenerationService(
        settings_repository=ImageGenerationSettingsRepository(
            lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
        ),
        llm_registry_gateway=image_generation_registry_gateway,
        file_resource_service=get_file_resource_service(),
    )
    codex_oauth_service = CodexOAuthFactory.create_service(llm_model_registry_gateway)
    codex_oauth_service.ensure_builtin_provider()
    codex_oauth_callback_server = CodexOAuthLocalCallbackServer(codex_oauth_service)
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
    event_loop_monitor = EventLoopLagMonitor()
    runtime_loop: asyncio.AbstractEventLoop | None = None
    detach_tasks: set[asyncio.Task[None]] = set()

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

    async def _detach_expired_scene(session) -> None:
        try:
            await asyncio.to_thread(
                agent_runtime_gateway.detach_scene,
                session.scene_id,
                DetachSceneRequest(workspace_path=session.workspace_path),
            )
        except Exception:
            logger.exception(
                "Scene 自动关闭失败: scene_id=%s",
                session.scene_id,
            )

    def _schedule_detach_expired_scene(session) -> None:
        task = asyncio.create_task(
            _detach_expired_scene(session),
            name=f"scene-session-expired-detach-{session.scene_id}",
        )
        detach_tasks.add(task)

        def _on_done(done_task: asyncio.Task[None]) -> None:
            detach_tasks.discard(done_task)
            with suppress(asyncio.CancelledError):
                done_task.result()

        task.add_done_callback(_on_done)

    def _on_scene_session_expired(session) -> None:
        loop = runtime_loop
        if loop is None or loop.is_closed():
            logger.warning("Scene 自动关闭已跳过，runtime event loop 不可用: scene_id=%s", session.scene_id)
            return
        try:
            loop.call_soon_threadsafe(_schedule_detach_expired_scene, session)
        except RuntimeError:
            logger.exception("Scene 自动关闭调度失败: scene_id=%s", session.scene_id)

    scene_session_registry = SceneSessionRegistry(
        on_expired=_on_scene_session_expired,
        can_expire=_can_scene_expire,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal runtime_loop

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

        runtime_loop = asyncio.get_running_loop()
        event_loop_monitor.start()
        runtime_network_monitor.start()
        scene_session_registry.start()
        codex_oauth_callback_server.start()
        auto_connect_task = asyncio.create_task(auto_connect_mcp_services())
        lacp_start_task = asyncio.create_task(lacp_client.start())
        try:
            yield
        finally:
            scene_session_registry.stop()
            for task in tuple(detach_tasks):
                task.cancel()
            for task in tuple(detach_tasks):
                with suppress(Exception, asyncio.CancelledError):
                    await task
            detach_tasks.clear()
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
            codex_oauth_callback_server.stop()
            await event_loop_monitor.stop()
            runtime_network_monitor.stop()
            runtime_loop = None

    app = FastAPI(title="spread-ai", description="通用 Agent 运行时环境服务", lifespan=lifespan)

    @app.middleware("http")
    async def network_monitoring_middleware(request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        trace_id = request.headers.get("x-network-trace-id") or request.query_params.get("network_trace_id") or new_network_trace_id("http")
        started_at = time.perf_counter()
        active_http = runtime_network_monitor.begin_http_request()
        path = _safe_network_path(str(request.url.path), dict(request.query_params))
        should_record_start = _is_key_network_path(path)
        if should_record_start:
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "http.request.start",
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "active_http_requests": active_http,
                **runtime_network_monitor.snapshot(),
            })
        try:
            response = await call_next(request)
        except Exception as exc:
            active_after = runtime_network_monitor.end_http_request()
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "http.request.error",
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "duration_ms": round((time.perf_counter() - started_at) * 1000),
                "active_http_requests": active_after,
                "event_loop": event_loop_monitor.snapshot(),
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
            })
            raise
        active_after = runtime_network_monitor.end_http_request()
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        if should_record_start or duration_ms >= 1000 or response.status_code >= 400:
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "http.request.end",
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "active_http_requests": active_after,
                "event_loop": event_loop_monitor.snapshot(),
                **runtime_network_monitor.snapshot(),
            })
        response.headers["x-network-trace-id"] = trace_id
        return response

    @app.get("/api/health")
    def health_check() -> dict[str, object]:
        return {
            "status": "ok",
            "event_loop": event_loop_monitor.snapshot(),
            "network": runtime_network_monitor.snapshot(),
        }

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
            communication_gateway=communication_gateway,
            runtime_health_snapshot=event_loop_monitor.snapshot,
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
        create_llm_model_registry_router(
            image_generation_registry_gateway,
            builtin_provider_ids=set(),
            api_formats=(API_FORMAT_OPENAI_IMAGES_GENERATIONS,),
            unsupported_input_modality_strategies=(
                UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE,
            ),
            readonly_models=True,
        ),
        prefix="/api/image-generation/llm-registry",
        tags=["图片生成 LLM 服务商与模型注册"],
    )
    app.include_router(
        create_image_generation_router(image_generation_service),
        prefix="/api/image-generation",
        tags=["图片生成"],
    )
    app.include_router(
        create_codex_oauth_router(codex_oauth_service),
        prefix="/api/codex-oauth",
        tags=["Codex OAuth"],
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


def _safe_network_path(path: str, query_params: dict[str, object]) -> str:
    allowed_keys = ("after_seq", "limit", "latest", "network_trace_id")
    safe_params = []
    for key in allowed_keys:
        value = query_params.get(key)
        if value is not None:
            safe_params.append(f"{key}={value}")
    return f"{path}?{'&'.join(safe_params)}" if safe_params else path


def _is_key_network_path(path: str) -> bool:
    return (
        path == "/api/health"
        or "diagnostics" in path
        or "attach" in path
        or "/events" in path
    )


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
