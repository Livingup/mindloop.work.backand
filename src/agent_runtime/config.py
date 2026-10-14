"""通用 Agent 运行时配置注册中心。

全局单例，统一加载 YAML 配置文件并分发给各业务域。
支持热更新：reload() 后所有消费方实时读到最新值。
配置文件启动时只读取原始 YAML；各业务配置段在被访问时才解析环境变量并构建强类型对象，
避免无关模块的必填环境变量阻塞 CLI 子命令启动。

使用方式：
    # 启动时加载一次
    ConfigRegistry.load("config/agent_runtime.yaml")

    # 任意位置实时读取
    cfg = ConfigRegistry.instance()
    cfg.file_resource_service        # → FileResourceServiceConfig
    cfg.conversation_repository      # → ConversationRepositoryConfig

    # 热更新（重新解析同一文件）
    ConfigRegistry.reload()
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

from agent_runtime.agent_definition.agent_definition_config import AgentDefinitionConfig
from agent_runtime.file_resource.config import FileResourceServiceConfig
from agent_runtime.mcp.mcp_client.mcp_client_config import McpClientConfig
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.skill.skill_config import SkillConfig
from agent_runtime.scene.scene_config import SceneConfig
from agent_runtime.conversation_message.config import ConversationRepositoryConfig
from agent_runtime.llm_model_registry.llm_model_registry_config import LlmModelRegistryConfig
from agent_runtime.llm_http_trace.config import LlmHttpTraceConfig
from agent_runtime.agent.agent_runtime_config import AgentRuntimeStateConfig
from agent_runtime.communication.communication_config import CommunicationConfig
from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.terminal.terminal_config import TerminalConfig
from agent_runtime.lacp.config import LacpConfig
from agent_runtime.codex_oauth.config import CodexOAuthConfig
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)


# ── 宿主运行时服务配置（全局，不属于任何业务域） ──────────────


@dataclass(frozen=True, slots=True)
class AgentRuntimeServiceConfig:
    """通用 Agent 运行时服务配置。

    用于表达当前运行时宿主服务自己的运行参数。
    """

    # 当前运行时服务实例名称。
    service_instance_name: str

    # 当前运行时服务环境标识（local / dev / test / prod）。
    service_environment: str

    # API 监听地址（bind address）：127.0.0.1 仅本机访问，0.0.0.0 允许通过本机所有网卡访问。
    service_host: str

    # 当前运行时服务监听端口。
    service_port: int


# ── 总配置聚合 ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    """通用 Agent 运行时总配置。

    聚合所有业务域的配置。frozen 保证单次加载后不可变，
    热更新通过整体替换实现（而非修改字段）。
    """

    # 运行时宿主服务配置。
    agent_runtime_service: AgentRuntimeServiceConfig

    # 统一文件资源服务配置。
    file_resource_service: FileResourceServiceConfig

    # 会话消息持久化仓库配置。
    conversation_repository: ConversationRepositoryConfig

    # 提示词模板服务配置。
    prompt_template: PromptTemplateConfig

    # Skill 服务配置。
    skill_service: SkillConfig

    # MCP 客户端服务配置。
    mcp_client: McpClientConfig

    # Agent 定义服务配置。
    agent_definition: AgentDefinitionConfig

    # Agent 运行态管理配置。
    agent_runtime: AgentRuntimeStateConfig = field(
        default_factory=AgentRuntimeStateConfig,
    )

    # 通用通信模块配置。
    communication: CommunicationConfig = field(
        default_factory=CommunicationConfig,
    )

    # Scene 模块配置（项目与场景）。
    scene: SceneConfig = field(
        default_factory=SceneConfig,
    )

    terminal: TerminalConfig = field(
        default_factory=TerminalConfig,
    )

    # LLM 服务商与模型注册模块配置。
    llm_model_registry: LlmModelRegistryConfig = field(
        default_factory=lambda: LlmModelRegistryConfig(
            registry_dir="~/.spread-ai/llm-model-registry",
        ),
    )

    llm_http_trace: LlmHttpTraceConfig = field(
        default_factory=LlmHttpTraceConfig,
    )

    context_compression: ContextCompressionConfig = field(
        default_factory=ContextCompressionConfig,
    )

    lacp: LacpConfig = field(
        default_factory=LacpConfig,
    )

    codex_oauth: CodexOAuthConfig = field(
        default_factory=CodexOAuthConfig,
    )

    cloud_user_config_database: CloudUserConfigDatabaseConfig = field(
        default_factory=CloudUserConfigDatabaseConfig,
    )


@dataclass(frozen=True, slots=True)
class ConfigRegistrySnapshot:
    yaml_path: str
    env_file_path: str | None
    raw_yaml: str
    raw_config_data: dict[str, Any]
    section_names: list[str]
    loaded_section_names: list[str]
    watch_enabled: bool


# ── 配置注册中心（全局单例） ─────────────────────────────────


class ConfigRegistry:
    """全局配置单例。

    内存中只有一份配置数据，所有消费方通过 instance() 实时读取。
    热更新调用 reload()，内部替换整个 frozen dataclass 引用。
    """

    _instance: ConfigRegistry | None = None
    _lock = threading.Lock()

    def __init__(
        self,
        config: AgentRuntimeConfig | dict[str, Any] | tuple[dict[str, Any], str],
        yaml_path: str,
        env_file_path: str | None = None,
    ) -> None:
        if isinstance(config, AgentRuntimeConfig):
            self._config: AgentRuntimeConfig | None = config
            self._raw_config_data: dict[str, Any] | None = None
            self._raw_yaml: str = ""
        elif isinstance(config, tuple):
            self._config = None
            self._raw_config_data = config[0]
            self._raw_yaml = config[1]
        else:
            self._config = None
            self._raw_config_data = config
            self._raw_yaml = yaml.safe_dump(
                config,
                allow_unicode=True,
                sort_keys=False,
            )
        self._yaml_path = yaml_path
        self._env_file_path = env_file_path
        self._section_cache: dict[str, Any] = {}
        self._watcher: _ConfigFileWatcher | None = None

    # ── 单例生命周期 ──────────────────────────────────────

    @classmethod
    def load(
        cls,
        yaml_file_path: str,
        env_file_path: str | None = None,
        *,
        watch: bool = False,
    ) -> None:
        """首次加载配置（应用启动时调用一次）。

        watch=True 时自动启动文件监听，YAML 文件保存后自动热更新。
        """
        with cls._lock:
            config_data = _load_yaml_config(yaml_file_path, env_file_path)
            cls._instance = cls(config_data, yaml_file_path, env_file_path)
            if watch:
                cls._instance._start_watching()

    @classmethod
    def reload(cls) -> None:
        """热更新：重新读取 YAML 文件，替换内部配置。"""
        with cls._lock:
            if cls._instance is None:
                raise RuntimeError("ConfigRegistry 尚未初始化，请先调用 load()。")
            raw_config_data, raw_yaml = _load_yaml_config(
                cls._instance._yaml_path,
                cls._instance._env_file_path,
            )
            cls._instance._raw_config_data = raw_config_data
            cls._instance._raw_yaml = raw_yaml
            cls._instance._config = None
            cls._instance._section_cache.clear()

    @classmethod
    def instance(cls) -> ConfigRegistry:
        """获取全局单例。未初始化时抛出 RuntimeError。"""
        inst = cls._instance
        if inst is None:
            raise RuntimeError("ConfigRegistry 尚未初始化，请先调用 load()。")
        return inst

    @classmethod
    def reset(cls) -> None:
        """重置单例（仅用于测试）。"""
        with cls._lock:
            if cls._instance is not None:
                cls._instance._stop_watching()
            cls._instance = None

    # ── 文件监听 ──────────────────────────────────────────

    def _start_watching(self) -> None:
        """启动 YAML 文件变更监听。"""
        if self._watcher is not None:
            return
        self._watcher = _ConfigFileWatcher(self._yaml_path)
        self._watcher.start()
        logger.info("已启动配置文件监听: %s", self._yaml_path)

    def _stop_watching(self) -> None:
        """停止文件监听。"""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            logger.info("已停止配置文件监听。")

    def snapshot(self) -> ConfigRegistrySnapshot:
        raw_config_data = self._raw_config_data
        if raw_config_data is None:
            raw_config_data = {}
        resolved_config_data = _resolve_env_vars(raw_config_data)
        return ConfigRegistrySnapshot(
            yaml_path=self._yaml_path,
            env_file_path=self._env_file_path,
            raw_yaml=self._raw_yaml,
            raw_config_data=dict(resolved_config_data),
            section_names=list(raw_config_data.keys()),
            loaded_section_names=sorted(self._section_cache.keys()),
            watch_enabled=self._watcher is not None,
        )

    # ── 配置属性（实时读取） ──────────────────────────────

    @property
    def agent_runtime_service(self) -> AgentRuntimeServiceConfig:
        """实时读取宿主运行时服务配置（实例名、环境、监听地址等）。"""
        if self._config is not None:
            return self._config.agent_runtime_service
        return self._get_section_config(
            "agent_runtime_service", _build_runtime_config,
        )

    @property
    def file_resource_service(self) -> FileResourceServiceConfig:
        """实时读取统一文件资源服务配置（存储路径、对象存储网关等）。"""
        if self._config is not None:
            return self._config.file_resource_service
        return self._get_section_config(
            "file_resource_service", _build_resource_config,
        )

    @property
    def conversation_repository(self) -> ConversationRepositoryConfig:
        """实时读取会话消息持久化仓储配置（数据目录等）。"""
        if self._config is not None:
            return self._config.conversation_repository
        return self._get_section_config(
            "conversation_repository", _build_conv_repo_config,
        )

    @property
    def prompt_template(self) -> PromptTemplateConfig:
        """实时读取提示词模板服务配置（片段存储目录等）。"""
        if self._config is not None:
            return self._config.prompt_template
        return self._get_section_config(
            "prompt_template", _build_prompt_template_config,
        )

    @property
    def skill_service(self) -> SkillConfig:
        """实时读取 Skill 服务配置（仓库目录路径）。"""
        if self._config is not None:
            return self._config.skill_service
        return self._get_section_config(
            "skill_service", _build_skill_config,
        )

    @property
    def mcp_client(self) -> McpClientConfig:
        """实时读取 MCP 客户端服务配置（服务定义目录等）。"""
        if self._config is not None:
            return self._config.mcp_client
        return self._get_section_config(
            "mcp_client", _build_mcp_client_config,
        )

    @property
    def agent_definition(self) -> AgentDefinitionConfig:
        """实时读取 Agent 定义服务配置（定义文件目录等）。"""
        if self._config is not None:
            return self._config.agent_definition
        return self._get_section_config(
            "agent_definition", _build_agent_definition_config,
        )

    @property
    def agent_runtime(self) -> AgentRuntimeStateConfig:
        """实时读取 Agent 运行态管理配置（仓储类型、数据目录等）。"""
        if self._config is not None:
            return self._config.agent_runtime
        return self._get_section_config(
            "agent_runtime", _build_agent_runtime_state_config,
        )

    @property
    def llm_model_registry(self) -> LlmModelRegistryConfig:
        """实时读取 LLM 服务商与模型注册模块配置（注册数据根目录等）。"""
        if self._config is not None:
            return self._config.llm_model_registry
        return self._get_section_config(
            "llm_model_registry", _build_llm_model_registry_config,
        )

    @property
    def llm_http_trace(self) -> LlmHttpTraceConfig:
        if self._config is not None:
            return self._config.llm_http_trace
        if self._raw_config_data is not None and "llm_http_trace" not in self._raw_config_data:
            return LlmHttpTraceConfig()
        return self._get_section_config(
            "llm_http_trace", _build_llm_http_trace_config,
        )

    @property
    def context_compression(self) -> ContextCompressionConfig:
        if self._config is not None:
            return self._config.context_compression
        if self._raw_config_data is not None and "context_compression" not in self._raw_config_data:
            return ContextCompressionConfig()
        return self._get_section_config(
            "context_compression", _build_context_compression_config,
        )

    @property
    def communication(self) -> CommunicationConfig:
        """实时读取通用通信模块配置（通信流与消息日志数据目录等）。"""
        if self._config is not None:
            return self._config.communication
        if self._raw_config_data is not None and "communication" not in self._raw_config_data:
            return CommunicationConfig()
        return self._get_section_config(
            "communication", _build_communication_config,
        )

    @property
    def scene(self) -> SceneConfig:
        """实时读取 Scene 模块配置（项目与场景 SQLite 数据目录）。"""
        if self._config is not None:
            return self._config.scene
        if self._raw_config_data is not None and "scene" not in self._raw_config_data:
            return SceneConfig()
        return self._get_section_config(
            "scene", _build_scene_config,
        )

    @property
    def lacp(self) -> LacpConfig:
        if self._config is not None:
            return self._config.lacp
        if self._raw_config_data is not None and "lacp" not in self._raw_config_data:
            return LacpConfig()
        return self._get_section_config(
            "lacp", _build_lacp_config,
        )

    @property
    def terminal(self) -> TerminalConfig:
        """实时读取 Terminal 模块配置（终端状态与输出数据目录等）。"""
        if self._config is not None:
            return self._config.terminal
        if self._raw_config_data is not None and "terminal" not in self._raw_config_data:
            return TerminalConfig()
        return self._get_section_config(
            "terminal", _build_terminal_config,
        )

    @property
    def codex_oauth(self) -> CodexOAuthConfig:
        if self._config is not None:
            return self._config.codex_oauth
        if self._raw_config_data is not None and "codex_oauth" not in self._raw_config_data:
            return CodexOAuthConfig()
        return self._get_section_config(
            "codex_oauth", _build_codex_oauth_config,
        )

    @property
    def cloud_user_config_database(self) -> CloudUserConfigDatabaseConfig:
        if self._config is not None:
            base_config = self._config.cloud_user_config_database
        elif self._raw_config_data is not None and "cloud_user_config_database" not in self._raw_config_data:
            base_config = CloudUserConfigDatabaseConfig()
        else:
            base_config = self._get_section_config(
                "cloud_user_config_database", _build_cloud_user_config_database_config,
            )
        return _apply_cloud_user_config_bootstrap(base_config)

    def _get_section_config(self, section_name: str, builder: Any) -> Any:
        """按需解析并构建单个配置段。

        ConfigRegistry 启动时只保存原始 YAML 数据。具体业务模块访问自己的配置属性时，
        才解析该配置段中的环境变量并构建强类型配置对象，避免无关配置段的必填环境变量阻塞 CLI。
        """
        if section_name in self._section_cache:
            return self._section_cache[section_name]
        if self._raw_config_data is None:
            raise RuntimeError("ConfigRegistry 缺少原始配置数据，无法按需解析配置段。")
        raw_section = _require_section(self._raw_config_data, section_name)
        resolved_section = _resolve_env_vars(raw_section)
        section_config = builder(resolved_section)
        self._section_cache[section_name] = section_config
        return section_config


# ── 文件监听实现 ─────────────────────────────────────────────


class _ConfigFileHandler(FileSystemEventHandler):
    """配置文件变更事件处理器。

    监听文件系统事件，当目标 YAML 配置文件被保存（修改）时，
    自动调用 ConfigRegistry.reload() 实现配置热更新。
    热更新失败时保留原有配置，仅记录异常日志。
    """

    def __init__(self, yaml_path: str) -> None:
        super().__init__()
        self._yaml_path = str(Path(yaml_path).resolve())

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        # 只响应目标 YAML 文件的修改事件。
        if str(Path(event.src_path).resolve()) != self._yaml_path:
            return
        logger.info("检测到配置文件变更，正在热更新: %s", event.src_path)
        try:
            ConfigRegistry.reload()
            logger.info("配置热更新成功。")
        except Exception:
            logger.exception("配置热更新失败，保留原有配置。")


class _ConfigFileWatcher:
    """配置文件监听器。

    封装 watchdog Observer 的启动、停止生命周期。
    以 daemon 线程运行，不阻塞进程退出。
    """

    def __init__(self, yaml_path: str) -> None:
        self._yaml_path = yaml_path
        self._observer = Observer()
        self._observer.daemon = True
        watch_dir = str(Path(yaml_path).resolve().parent)
        self._observer.schedule(
            _ConfigFileHandler(yaml_path), path=watch_dir, recursive=False,
        )

    def start(self) -> None:
        """启动文件监听线程。"""
        self._observer.start()

    def stop(self) -> None:
        """停止文件监听线程并等待其结束。"""
        self._observer.stop()
        self._observer.join(timeout=2)


# ── YAML 加载 ────────────────────────────────────────────────

# 匹配 ${ENV_VAR} 或 ${ENV_VAR:-default} 格式的环境变量占位符。
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _load_yaml_config(
    yaml_file_path: str,
    env_file_path: str | None = None,
) -> tuple[dict[str, Any], str]:
    """从 YAML 文件读取原始总配置。

    注意：此处不递归解析环境变量，也不构建所有配置段。
    具体配置段在 ConfigRegistry 对应 property 被访问时才按需解析和构建。
    """

    config_path = Path(yaml_file_path)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件: {yaml_file_path}")

    _load_dot_env(config_path, env_file_path)

    raw_yaml = config_path.read_text(encoding="utf-8")
    config_data = yaml.safe_load(raw_yaml) or {}
    if not isinstance(config_data, dict):
        raise ValueError("配置文件内容必须是对象结构。")
    return config_data, raw_yaml


def _build_runtime_config(data: dict[str, Any]) -> AgentRuntimeServiceConfig:
    """从 YAML 字典构建宿主运行时服务配置。"""
    return AgentRuntimeServiceConfig(
        service_instance_name=str(data["service_instance_name"]),
        service_environment=str(data["service_environment"]),
        service_host=str(data["service_host"]),
        service_port=int(data["service_port"]),
    )


def _build_resource_config(data: dict[str, Any]) -> FileResourceServiceConfig:
    """从 YAML 字典构建统一文件资源服务配置。"""
    return FileResourceServiceConfig(
        file_resource_batch_limit=int(data["file_resource_batch_limit"]),
        local_resource_storage_root_path=str(data["local_resource_storage_root_path"]),
    )


def _build_prompt_template_config(data: dict[str, Any]) -> PromptTemplateConfig:
    """从 YAML 字典构建提示词模板服务配置。"""
    return PromptTemplateConfig(
        snippets_dir=str(data["snippets_dir"]),
    )


def _build_skill_config(data: dict[str, Any]) -> SkillConfig:
    """从 YAML 字典构建 Skill 服务配置。"""
    kwargs: dict[str, Any] = {"skills_dir": str(data["skills_dir"])}
    if "skill_groups_file" in data:
        kwargs["skill_groups_file"] = str(data["skill_groups_file"])
    if "settings_dir" in data:
        kwargs["settings_dir"] = str(data["settings_dir"])
    if "auto_refresh_enabled" in data:
        kwargs["auto_refresh_enabled"] = bool(data["auto_refresh_enabled"])
    if "snapshot_check_interval_seconds" in data:
        kwargs["snapshot_check_interval_seconds"] = float(data["snapshot_check_interval_seconds"])
    return SkillConfig(**kwargs)


def _build_mcp_client_config(data: dict[str, Any]) -> McpClientConfig:
    """从 YAML 字典构建 MCP 客户端服务配置。"""
    return McpClientConfig(
        config_dir=str(data["config_dir"]),
    )


def _build_agent_definition_config(data: dict[str, Any]) -> AgentDefinitionConfig:
    """从 YAML 字典构建 Agent 定义服务配置。"""
    return AgentDefinitionConfig(
        definitions_dir=str(data["definitions_dir"]),
    )


def _build_agent_runtime_state_config(data: dict[str, Any]) -> AgentRuntimeStateConfig:
    """从 YAML 字典构建 Agent 运行态管理配置。"""
    return AgentRuntimeStateConfig(
        repository_type=str(data.get("repository_type", "sqlite")),
        data_dir=str(data.get("data_dir", "~/.spread-ai/agent-runtime")),
        trace_dir=str(data.get("trace_dir", "~/.spread-ai/agent-traces")),
    )


def _build_llm_model_registry_config(data: dict[str, Any]) -> LlmModelRegistryConfig:
    """从 YAML 字典构建 LLM 服务商与模型注册模块配置。"""
    return LlmModelRegistryConfig(
        registry_dir=str(data["registry_dir"]),
    )


def _build_cloud_user_config_database_config(data: dict[str, Any]) -> CloudUserConfigDatabaseConfig:
    pool_data = data.get("pool", {})
    if not isinstance(pool_data, dict):
        pool_data = {}
    return CloudUserConfigDatabaseConfig(
        enabled=bool(data.get("enabled", False)),
        backend=str(data.get("backend", "local")),
        host=str(data.get("host", "")),
        port=int(data.get("port", 5432)),
        database=str(data.get("database", "")),
        username=str(data.get("username", "")),
        password=str(data.get("password", "")),
        sslmode=str(data.get("sslmode", "require")),
        bootstrap_data_dir=str(
            data.get("bootstrap_data_dir", "~/.spread-ai/cloud-user-config")
        ),
        pool=CloudUserConfigDatabasePoolConfig(
            min_size=int(pool_data.get("min_size", 1)),
            max_size=int(pool_data.get("max_size", 5)),
            connect_timeout_seconds=int(
                pool_data.get("connect_timeout_seconds", 10)
            ),
        ),
    )


def _apply_cloud_user_config_bootstrap(
    base_config: CloudUserConfigDatabaseConfig,
) -> CloudUserConfigDatabaseConfig:
    """只在机器本地 durable bootstrap 完成后叠加 PostgreSQL 凭据。

    保存连接凭据本身不等于 Runtime 已可进入完整共享配置模式；必须同时存在匹配的本机
    共享配置快照、OSS 已配置或显式跳过、以及本机完成标记，才能把 PostgreSQL 连接
    暴露给普通共享配置仓储。这样首启/未完成引导保持后端本地限制模式，避免桌面启动路径
    被同步 PostgreSQL 访问阻塞。
    """
    try:
        from agent_runtime.cloud_user_config.bootstrap_settings import (
            CloudUserConfigBootstrapSettingsRepository,
        )
        from agent_runtime.cloud_user_config.local_state import CloudUserConfigLocalStateRepository

        repository = CloudUserConfigBootstrapSettingsRepository(
            lambda: base_config.bootstrap_data_dir,
        )
        settings = repository.get_settings()
        if settings is None or not settings.configured:
            return base_config
        effective_config = settings.to_database_config(base_config)
        readiness = CloudUserConfigLocalStateRepository(
            lambda: base_config.bootstrap_data_dir,
        ).get_readiness(
            credentials_saved=True,
            expected_config_key=_cloud_user_config_database_key(effective_config),
        )
        if not readiness.completed:
            if readiness.error_message:
                logger.warning(
                    "云端用户配置 bootstrap 本机状态未就绪，继续使用 YAML 默认值: %s",
                    readiness.error_message,
                )
            return base_config
        return effective_config
    except Exception:
        logger.exception("读取云端用户配置 bootstrap 状态失败，继续使用 YAML 默认值。")
        return base_config


def _cloud_user_config_database_key(config: CloudUserConfigDatabaseConfig) -> tuple[object, ...]:
    """生成本机共享配置快照与 PostgreSQL 连接配置的匹配键，不包含密码。"""
    return (
        config.host,
        config.port,
        config.database,
        config.username,
        config.sslmode,
    )


def _build_llm_http_trace_config(data: dict[str, Any]) -> LlmHttpTraceConfig:
    return LlmHttpTraceConfig(
        enabled=bool(data.get("enabled", True)),
        data_dir=str(data.get("data_dir", "~/.spread-ai/llm-http-traces")),
    )


def _build_context_compression_config(data: dict[str, Any]) -> ContextCompressionConfig:
    raw_input_mode = str(data.get("input_mode", "native_multimodal_history")).strip()
    # 旧配置 transcript_snapshot 一律收敛到 native_multimodal_history。
    if raw_input_mode in {"", "transcript_snapshot"}:
        raw_input_mode = "native_multimodal_history"
    return ContextCompressionConfig(
        enabled=bool(data.get("enabled", True)),
        default_threshold_ratio=float(data.get("default_threshold_ratio", 0.75)),
        max_worker_rounds=int(data.get("max_worker_rounds", data.get("section_worker_max_rounds", 128))),
        input_mode=raw_input_mode,
        verifier_enabled=bool(data.get("verifier_enabled", False)),
        archive_data_dir=str(data.get("archive_data_dir", "~/.spread-ai/context-compression")),
        observer_trigger_tokens=int(data.get("observer_trigger_tokens", 80000)),
        observer_min_advance_ratio=float(data.get("observer_min_advance_ratio", 0.5)),
    )


def _build_communication_config(data: dict[str, Any]) -> CommunicationConfig:
    """从 YAML 字典构建通用通信模块配置。"""
    storage_mode = str(data.get("storage_mode", "memory")).strip().lower()
    if storage_mode not in ("memory", "sqlite"):
        storage_mode = "memory"
    max_events = int(data.get("memory_ring_max_events", data.get("replay_buffer_size", 4000)))
    return CommunicationConfig(
        data_dir=str(data.get("data_dir", "~/.spread-ai/communication")),
        replay_buffer_size=int(data.get("replay_buffer_size", max_events)),
        subscriber_queue_size=int(data.get("subscriber_queue_size", 1000)),
        storage_mode=storage_mode,  # type: ignore[arg-type]
        memory_ring_max_events=max_events,
        memory_ring_max_bytes=int(data.get("memory_ring_max_bytes", 24 * 1024 * 1024)),
        memory_ring_main_min_events=int(data.get("memory_ring_main_min_events", 1000)),
        memory_ring_terminal_max_events=int(data.get("memory_ring_terminal_max_events", 3000)),
        global_memory_ring_max_bytes=int(data.get("global_memory_ring_max_bytes", 2560 * 1024 * 1024)),
        stream_idle_reclaim_minutes=int(data.get("stream_idle_reclaim_minutes", 30)),
        stream_idle_shrink_events=int(data.get("stream_idle_shrink_events", 64)),
        idle_scan_interval_seconds=int(data.get("idle_scan_interval_seconds", 60)),
    )


def _build_lacp_config(data: dict[str, Any]) -> LacpConfig:
    return LacpConfig(
        enabled=bool(data.get("enabled", False)),
        registry_url=str(data.get("registry_url", "")),
        api_key=(str(data["api_key"]) if data.get("api_key") is not None else None),
        data_dir=str(data.get("data_dir", "~/.spread-ai/lacp")),
        connect_timeout_seconds=float(data.get("connect_timeout_seconds", 10.0)),
        request_timeout_seconds=float(data.get("request_timeout_seconds", 5.0)),
        reconnect_initial_delay_seconds=float(data.get("reconnect_initial_delay_seconds", 1.0)),
        reconnect_max_delay_seconds=float(data.get("reconnect_max_delay_seconds", 30.0)),
    )


def _build_terminal_config(data: dict[str, Any]) -> TerminalConfig:
    max_chars = data.get("output_coalesce_max_chars", data.get("output_coalesce_max_bytes", 16_384))
    return TerminalConfig(
        data_dir=str(data.get("data_dir", "~/.spread-ai/terminal")),
        default_shell=(
            str(data["default_shell"])
            if data.get("default_shell") is not None
            else None
        ),
        max_output_chars_per_read=int(data.get("max_output_chars_per_read", 100_000)),
        max_live_terminals=int(data.get("max_live_terminals", 200)),
        max_live_terminals_per_agent=int(data.get("max_live_terminals_per_agent", 16)),
        output_coalesce_interval_ms=int(data.get("output_coalesce_interval_ms", 50)),
        output_coalesce_max_chars=int(max_chars),
        output_coalesce_max_chunks=int(data.get("output_coalesce_max_chunks", 32)),
        output_coalesce_max_bytes=int(data.get("output_coalesce_max_bytes", max_chars)),
        output_ring_max_chunks=int(data.get("output_ring_max_chunks", 1000)),
        output_ring_max_chars=int(data.get("output_ring_max_chars", 100_000)),
    )


def _build_codex_oauth_config(data: dict[str, Any]) -> CodexOAuthConfig:
    return CodexOAuthConfig(
        data_dir=str(data.get("data_dir", "~/.spread-ai/louyi-codex")),
    )


def _build_scene_config(data: dict[str, Any]) -> SceneConfig:
    """从 YAML 字典构建 Scene 模块配置。"""
    return SceneConfig(
        scenes_db_path=str(
            data.get("scenes_db_path", "~/.spread-ai/scene/scene.db")
        ),
        recent_workspaces_path=str(
            data.get("recent_workspaces_path", "~/.spread-ai/recent-workspaces.json")
        ),
    )


def _build_conv_repo_config(data: dict[str, Any]) -> ConversationRepositoryConfig:
    """从 YAML 字典构建会话消息持久化仓储配置。"""
    return ConversationRepositoryConfig(
        data_dir=str(data["data_dir"]),
    )


# ── .env 加载 + 环境变量解析 ─────────────────────────────────


def _load_dot_env(yaml_config_path: Path, env_file_path: str | None) -> None:
    """加载显式指定的 env 文件中的程序启动元数据覆盖。

    新配置边界下，YAML 同目录的 `.env` 不再被自动加载，避免 repo/env 文件
    悄悄承载 OSS、云 DB 密码等用户业务密钥。开发环境如需覆盖 service host/port/env，
    必须通过 `--env-file config/.env.dev` 显式传入。
    """
    _ = yaml_config_path
    protected_keys = set(os.environ)
    if env_file_path is not None:
        _load_single_dot_env(Path(env_file_path), protected_keys=protected_keys, override=True)


def _load_single_dot_env(dot_env_path: Path, *, protected_keys: set[str], override: bool) -> None:
    if not dot_env_path.exists():
        return

    try:
        from dotenv import dotenv_values
        values = dotenv_values(dot_env_path)
    except ImportError:
        values = _parse_dot_env_file(dot_env_path)
    for key, value in values.items():
        if key in protected_keys or value is None:
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = str(value)


def _parse_dot_env_file(dot_env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in dot_env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def _resolve_env_vars(value: Any) -> Any:
    """递归解析配置值中的 ${ENV_VAR} 占位符，并展开 ~ 为用户主目录。

    支持格式：
    - ${ENV_VAR}          — 必须存在，不存在则报错
    - ${ENV_VAR:-default}  — 不存在时使用缺省值
    - ~/path              — 展开为用户主目录（跨平台）
    """
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            env_key = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(env_key)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ValueError(
                f"环境变量 {env_key} 未设置，且未指定缺省值。"
                f"请在 .env 文件或系统环境变量中配置。"
            )
        resolved = _ENV_VAR_PATTERN.sub(_replace, value)
        # 展开 ~ 为用户主目录（跨平台：Windows → %USERPROFILE%，Unix → $HOME）。
        return _normalize_path_like_config_value(os.path.expanduser(resolved))
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _normalize_path_like_config_value(value: str) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return value
    if os.path.isabs(value) or re.match(r"^[a-zA-Z]:[\\/]", value):
        return os.path.normpath(value)
    return value


def _require_section(root: dict[str, Any], section_name: str) -> dict[str, Any]:
    """要求指定配置段必须存在且为对象结构。"""
    section = root.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段 {section_name} 必须存在且必须是对象结构。")
    return section
