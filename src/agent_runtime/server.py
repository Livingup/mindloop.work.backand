from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import RLock

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from agent_runtime.config import ConfigRegistry

logger = logging.getLogger("agent_runtime.serve")

# 所有前端离开后，只有连续 30 分钟没有新的任务/执行活动，才允许自动释放 scene。
SCENE_VACANT_IDLE_EXPIRATION_SECONDS: float = 30 * 60


class _SwitchableRuntimeComponent:
    """线程安全的运行时组件代理。

    bootstrap 首启时普通 API 被限制，但路由和服务对象仍可能已经持有局部组件引用。
    该代理允许 durable readiness 完成后在同一进程把这些引用切换到完整模式组件，
    避免继续使用启动时的本地 bootstrap 仓储。
    """

    def __init__(self, target: object) -> None:
        self._target = target
        self._lock = RLock()

    @property
    def target(self) -> object:
        with self._lock:
            return self._target

    def set_target(self, target: object) -> None:
        with self._lock:
            self._target = target

    def __getattr__(self, name: str):
        return getattr(self.target, name)


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
    from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_IMAGES_GENERATIONS, API_FORMAT_OPENAI_RESPONSES
    from agent_runtime.llm_http_trace.llm_http_trace_factory import (
        LlmHttpTraceFactory,
    )
    from agent_runtime.llm_http_trace.north_gateway.llm_http_trace_rest_adapter import (
        create_llm_http_trace_router,
    )
    from agent_runtime.llm_client import LlmClientFactory
    from agent_runtime.image_generation.north_gateway.image_generation_rest_adapter import (
        create_image_generation_router,
    )
    from agent_runtime.image_generation.service import ImageGenerationService
    from agent_runtime.image_generation.settings_repository import create_image_generation_settings_repository
    from agent_runtime.web_tools.north_gateway.web_tools_rest_adapter import (
        create_web_tools_router,
    )
    from agent_runtime.web_tools.service import WebToolsService
    from agent_runtime.web_tools.settings_repository import create_web_tools_settings_repository
    from agent_runtime.file_resource.shared_service import (
        get_file_resource_service,
        reset_file_resource_services_for_runtime_mode_change,
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
        create_context_compression_worker_prompt_repository,
    )
    from agent_runtime.context_compression.settings_repository import (
        create_context_compression_settings_repository,
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
    from agent_runtime.auth.factory import RuntimeAuthFactory
    from agent_runtime.auth.north_gateway.auth_rest_adapter import create_runtime_auth_router
    from agent_runtime.cloud_user_config.north_gateway.bootstrap_rest_adapter import (
        create_runtime_bootstrap_router,
    )
    from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
    from agent_runtime.logs_management.service import LogsManagementService
    from agent_runtime.logs_management.north_gateway.logs_management_rest_adapter import (
        create_logs_management_router,
    )
    from agent_runtime.runtime_health import EventLoopLagMonitor
    from agent_runtime.runtime_network_monitor import (
        new_network_trace_id,
        runtime_network_monitor,
    )

    cloud_user_config_database_active_at_start = (
        ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled
    )
    runtime_full_mode_active = cloud_user_config_database_active_at_start
    runtime_full_mode_lock = RLock()

    prompt_gateway = _SwitchableRuntimeComponent(PromptTemplateFactory.create())
    tool_gateway = LocalToolFactory.create()
    skill_gateway = SkillFactory.create()
    mcp_gateway = _SwitchableRuntimeComponent(McpClientFactory.create())
    from agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools import (
        configure as configure_mcp_meta_tools,
    )
    configure_mcp_meta_tools(mcp_gateway)
    agent_definition_gateway = _SwitchableRuntimeComponent(AgentDefinitionFactory.create())
    communication_gateway = CommunicationFactory.create()
    chat_bubble_service = ChatBubbleFactory.create()
    conversation_repository = SqliteConversationRepository(
        ConfigRegistry.instance().conversation_repository.data_dir,
    )
    context_compression_prompt_repository = create_context_compression_worker_prompt_repository(
        lambda: ConfigRegistry.instance().context_compression.archive_data_dir,
    )
    context_compression_settings_repository = create_context_compression_settings_repository(
        lambda: ConfigRegistry.instance().context_compression.archive_data_dir,
    )
    scene_gateway = SceneFactory.create()
    agent_runtime_gateway = AgentRuntimeFactory.create(
        scene_gateway=scene_gateway,
        communication_gateway=communication_gateway,
    )
    llm_model_registry_gateway = _SwitchableRuntimeComponent(LlmModelRegistryFactory.create())
    image_generation_registry_gateway = _SwitchableRuntimeComponent(
        LlmModelRegistryFactory.create_image_generation_registry()
    )
    file_resource_service = _SwitchableRuntimeComponent(get_file_resource_service())
    image_generation_service = _SwitchableRuntimeComponent(
        ImageGenerationService(
            settings_repository=create_image_generation_settings_repository(
                lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
            ),
            llm_registry_gateway=image_generation_registry_gateway,
            file_resource_service=file_resource_service,
        )
    )
    web_tools_service = _SwitchableRuntimeComponent(
        WebToolsService(
            settings_repository=create_web_tools_settings_repository(
                lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
            ),
            llm_registry_gateway=llm_model_registry_gateway,
            llm_request_service=LlmClientFactory.create(),
        )
    )
    codex_oauth_service = CodexOAuthFactory.create_service(llm_model_registry_gateway)
    if not cloud_user_config_database_active_at_start:
        logger.info("Runtime 处于 bootstrap 限制模式，跳过内置 Codex Provider 写入以避免本地 LLM Registry 成为共享配置权威。")
    codex_oauth_callback_server = CodexOAuthLocalCallbackServer(codex_oauth_service)
    runtime_auth_service = RuntimeAuthFactory.create_service()
    llm_http_trace_service = LlmHttpTraceFactory.create()
    agent_execution_engine = _SwitchableRuntimeComponent(
        AgentExecutionFactory.create(
            mcp_client_gateway=mcp_gateway,
        )
    )
    lacp_bridge = _SwitchableRuntimeComponent(
        LacpFactory.create_bridge(
            runtime_gateway=agent_runtime_gateway,
            agent_definition_gateway=agent_definition_gateway,
            execution_engine=agent_execution_engine,
        )
    )
    lacp_client = LacpFactory.create_client()
    lacp_settings_repository = _SwitchableRuntimeComponent(LacpFactory.create_settings_repository())
    terminal_gateway = TerminalFactory.create()
    logs_management_service = LogsManagementService()
    system_settings_gateway = SystemSettingsFactory.create()
    event_loop_monitor = EventLoopLagMonitor()
    runtime_loop: asyncio.AbstractEventLoop | None = None
    detach_tasks: set[asyncio.Task[None]] = set()
    auto_connect_task: asyncio.Task | None = None
    lacp_start_task: asyncio.Task | None = None
    config_cache_poll_task: asyncio.Task | None = None
    codex_provider_task: asyncio.Task | None = None
    codex_oauth_callback_server_started = False

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

    async def ensure_codex_builtin_provider() -> None:
        try:
            await asyncio.to_thread(codex_oauth_service.ensure_builtin_provider)
        except (Exception, asyncio.CancelledError) as e:
            logger.warning("内置 Codex Provider 后台同步失败，Runtime 将继续运行：%s", e, exc_info=True)

    def _rebuild_full_mode_components() -> None:
        nonlocal lacp_client

        reset_file_resource_services_for_runtime_mode_change()
        LacpFactory.reset_for_runtime_mode_change()

        prompt_gateway.set_target(PromptTemplateFactory.create())
        mcp_gateway.set_target(McpClientFactory.create())
        configure_mcp_meta_tools(mcp_gateway)
        agent_definition_gateway.set_target(AgentDefinitionFactory.create())
        llm_model_registry_gateway.set_target(LlmModelRegistryFactory.create())
        image_generation_registry_gateway.set_target(
            LlmModelRegistryFactory.create_image_generation_registry()
        )
        file_resource_service.set_target(get_file_resource_service())
        image_generation_service.set_target(
            ImageGenerationService(
                settings_repository=create_image_generation_settings_repository(
                    lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
                ),
                llm_registry_gateway=image_generation_registry_gateway,
                file_resource_service=file_resource_service,
            )
        )
        web_tools_service.set_target(
            WebToolsService(
                settings_repository=create_web_tools_settings_repository(
                    lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
                ),
                llm_registry_gateway=llm_model_registry_gateway,
                llm_request_service=LlmClientFactory.create(),
            )
        )
        agent_execution_engine.set_target(
            AgentExecutionFactory.create(
                mcp_client_gateway=mcp_gateway,
            )
        )
        lacp_client = LacpFactory.create_client()
        lacp_bridge.set_target(
            LacpFactory.create_bridge(
                runtime_gateway=agent_runtime_gateway,
                agent_definition_gateway=agent_definition_gateway,
                execution_engine=agent_execution_engine,
            )
        )
        lacp_settings_repository.set_target(LacpFactory.create_settings_repository())

    def _activate_runtime_full_mode_if_ready() -> bool:
        nonlocal runtime_full_mode_active

        if runtime_full_mode_active:
            return True
        if not ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
            return False
        with runtime_full_mode_lock:
            if runtime_full_mode_active:
                return True
            if not ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
                return False
            _rebuild_full_mode_components()
            runtime_full_mode_active = True
        _schedule_full_mode_background_services()
        logger.info("Runtime bootstrap durable readiness 已完成，已在当前进程切换到完整共享配置模式。")
        return True

    def _schedule_full_mode_background_services() -> None:
        loop = runtime_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(_start_full_mode_background_services_on_loop)
        except RuntimeError:
            logger.exception("Runtime 完整模式后台服务调度失败。")

    def _start_full_mode_background_services_on_loop() -> None:
        nonlocal auto_connect_task, lacp_start_task, config_cache_poll_task
        nonlocal codex_provider_task, codex_oauth_callback_server_started

        if config_cache_poll_task is None or config_cache_poll_task.done():
            config_cache_poll_task = PostgresUserConfigStore.start_background_revision_polling(
                lambda: ConfigRegistry.instance().cloud_user_config_database,
            )
        if not codex_oauth_callback_server_started:
            codex_oauth_callback_server.start()
            codex_oauth_callback_server_started = True
        if codex_provider_task is None or codex_provider_task.done():
            codex_provider_task = asyncio.create_task(
                ensure_codex_builtin_provider(),
                name="codex-oauth-builtin-provider-sync",
            )
        if auto_connect_task is None or auto_connect_task.done():
            auto_connect_task = asyncio.create_task(auto_connect_mcp_services())
        if lacp_start_task is None or lacp_start_task.done():
            lacp_start_task = asyncio.create_task(lacp_client.start())

    from agent_runtime.agent.north_gateway.agent_runtime_contracts import DetachSceneRequest
    from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING

    def _timestamp_from_datetime(value) -> float | None:
        if value is None:
            return None
        try:
            return value.timestamp()
        except Exception:
            return None

    def _max_activity_timestamp(*values) -> float | None:
        timestamps = [
            timestamp
            for timestamp in (_timestamp_from_datetime(value) for value in values)
            if timestamp is not None
        ]
        return max(timestamps) if timestamps else None

    def _can_scene_expire(session) -> bool:
        """判断空置 scene 是否已满足自动释放门禁。

        自动释放必须同时满足：scene 没有任何前端客户端、空置已持续 30 分钟，且 task/agent
        在这 30 分钟内没有新的运行态活动。检查异常时保守拒绝过期，避免误停后台任务。
        """
        now = time.time()
        vacant_since_at = getattr(session, "vacant_since_at", None)
        if vacant_since_at is None:
            return False
        if now - vacant_since_at < SCENE_VACANT_IDLE_EXPIRATION_SECONDS:
            return False
        try:
            tasks = agent_runtime_gateway.list_scene_tasks(session.scene_id)
            latest_activity_at = vacant_since_at
            for task in tasks:
                task_activity_at = _max_activity_timestamp(
                    task.created_at,
                    task.updated_at,
                    task.started_at,
                    task.paused_at,
                    task.finished_at,
                    task.failed_at,
                )
                if task_activity_at is not None:
                    latest_activity_at = max(latest_activity_at, task_activity_at)
                for instance_id in task.agent_instance_ids:
                    try:
                        instance = agent_runtime_gateway.get_agent_instance(instance_id)
                    except KeyError:
                        continue
                    if instance.status == AGENT_INSTANCE_STATUS_RUNNING:
                        return False
                    instance_activity_at = _max_activity_timestamp(
                        instance.created_at,
                        instance.updated_at,
                        instance.started_at,
                        instance.paused_at,
                        instance.finished_at,
                        instance.failed_at,
                    )
                    if instance_activity_at is not None:
                        latest_activity_at = max(latest_activity_at, instance_activity_at)
            return now - latest_activity_at >= SCENE_VACANT_IDLE_EXPIRATION_SECONDS
        except Exception:
            logger.exception("Scene 自动释放条件判断失败，保守跳过本轮: scene_id=%s", session.scene_id)
            return False

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

        runtime_loop = asyncio.get_running_loop()
        event_loop_monitor.start()
        runtime_network_monitor.start()
        scene_session_registry.start()
        if runtime_full_mode_active:
            _start_full_mode_background_services_on_loop()
        else:
            logger.info("Runtime 处于 bootstrap 限制模式，跳过 MCP 自动连接、LACP 启动与 Codex OAuth 回调服务。")
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
            if auto_connect_task is not None and not auto_connect_task.done():
                auto_connect_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await auto_connect_task
            if lacp_start_task is not None and not lacp_start_task.done():
                lacp_start_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await lacp_start_task
            if config_cache_poll_task is not None and not config_cache_poll_task.done():
                config_cache_poll_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await config_cache_poll_task
            if codex_provider_task is not None and not codex_provider_task.done():
                codex_provider_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await codex_provider_task
            await lacp_client.shutdown()
            await mcp_gateway.shutdown()
            if codex_oauth_callback_server_started:
                codex_oauth_callback_server.stop()
            await event_loop_monitor.stop()
            runtime_network_monitor.stop()
            runtime_loop = None

    app = FastAPI(title="spread-ai", description="通用 Agent 运行时环境服务", lifespan=lifespan)

    @app.middleware("http")
    async def runtime_auth_middleware(request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or _is_runtime_auth_exempt_path(path):
            return await call_next(request)
        if not runtime_auth_service.has_accounts():
            return await call_next(request)
        auth_session = runtime_auth_service.authenticate_token(_runtime_auth_token_from_request(request))
        if auth_session is None:
            return JSONResponse(
                {"detail": "未登录或登录已过期，请重新登录。"},
                status_code=401,
            )
        request.state.runtime_auth_session = auth_session
        return await call_next(request)

    @app.middleware("http")
    async def runtime_bootstrap_guard_middleware(request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not _is_runtime_bootstrap_exempt_path(path):
            try:
                full_mode_ready = _activate_runtime_full_mode_if_ready()
            except Exception as exc:  # noqa: BLE001 - 请求守卫需要把同进程激活失败稳定暴露给前端
                logger.exception("Runtime bootstrap 已完成但完整模式激活失败。")
                return JSONResponse(
                    {
                        "detail": f"Runtime bootstrap 已完成，但当前进程切换完整模式失败：{exc}",
                        "bootstrap_required": False,
                        "restricted": True,
                    },
                    status_code=503,
                )
            if not full_mode_ready:
                return JSONResponse(
                    {
                        "detail": "Runtime 尚未完成本机 bootstrap durable readiness；请先完成 /api/runtime-bootstrap 引导。",
                        "bootstrap_required": True,
                        "restricted": True,
                    },
                    status_code=503,
                )
        return await call_next(request)

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

    # Starlette 后添加的 middleware 会成为更外层包装；CORS 必须包在认证与 bootstrap guard 外侧，
    # 否则 Electron renderer 的 OPTIONS 预检不带登录 token 时会先被认证中间件拦截为 401。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-network-trace-id"],
        allow_credentials=False,
    )

    @app.get("/api/health")
    def health_check() -> dict[str, object]:
        return {
            "status": "ok",
            "event_loop": event_loop_monitor.snapshot(),
            "network": runtime_network_monitor.snapshot(),
        }

    app.include_router(
        create_runtime_auth_router(runtime_auth_service),
        prefix="/api/auth",
        tags=["运行时账号认证"],
    )
    app.include_router(
        create_runtime_bootstrap_router(
            cloud_user_config_database_active_at_start=cloud_user_config_database_active_at_start,
            on_bootstrap_ready=_activate_runtime_full_mode_if_ready,
        ),
        prefix="/api/runtime-bootstrap",
        tags=["Runtime 初始化引导"],
    )
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
            file_resource_service=file_resource_service,
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
            api_formats=(API_FORMAT_OPENAI_IMAGES_GENERATIONS, API_FORMAT_OPENAI_RESPONSES),
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
        create_web_tools_router(web_tools_service),
        prefix="/api/web-tools",
        tags=["网络工具"],
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
        create_logs_management_router(logs_management_service),
        prefix="/api/logs",
        tags=["日志工作区"],
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


def _is_runtime_auth_exempt_path(path: str) -> bool:
    return path == "/api/health" or path.startswith("/api/auth/")


def _is_runtime_bootstrap_exempt_path(path: str) -> bool:
    return (
        path == "/api/health"
        or path.startswith("/api/auth/")
        or path.startswith("/api/runtime-bootstrap/")
        or path.startswith("/api/system-settings")
    )


def _runtime_auth_token_from_request(request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            token = authorization[len(prefix):].strip()
            if token:
                return token
    if _runtime_auth_query_token_allowed(request):
        query_token = request.query_params.get("access_token")
        if isinstance(query_token, str) and query_token.strip():
            return query_token.strip()
    return None


def _runtime_auth_query_token_allowed(request) -> bool:
    path = getattr(getattr(request, "url", None), "path", "") or ""
    method = (getattr(request, "method", "") or "").upper()
    if method == "GET" and path.startswith("/api/communication/streams/") and path.endswith("/events"):
        return True
    if method == "GET" and path.startswith("/api/file-resources/upload-tasks/") and path.endswith("/events"):
        return True
    if method == "GET" and path.startswith("/api/file-resources/") and path.endswith("/content"):
        return True
    if method == "POST" and path.startswith("/api/agent-runtime/scenes/") and path.endswith("/detach"):
        return True
    return False


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
