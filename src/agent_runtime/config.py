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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

from agent_runtime.agent_definition.agent_definition_config import AgentDefinitionConfig
from agent_runtime.file_resource.config import (
    FileResourceServiceConfig,
    ObjectStorageGatewayConfig,
)
from agent_runtime.mcp.mcp_client.mcp_client_config import McpClientConfig
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.skill.skill_config import SkillConfig
from agent_runtime.conversation_message.config import ConversationRepositoryConfig


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

    # 当前运行时服务监听地址。
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
        config: AgentRuntimeConfig | dict[str, Any],
        yaml_path: str,
        env_file_path: str | None = None,
    ) -> None:
        if isinstance(config, AgentRuntimeConfig):
            self._config: AgentRuntimeConfig | None = config
            self._raw_config_data: dict[str, Any] | None = None
        else:
            self._config = None
            self._raw_config_data = config
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
            cls._instance._raw_config_data = _load_yaml_config(
                cls._instance._yaml_path,
                cls._instance._env_file_path,
            )
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
) -> dict[str, Any]:
    """从 YAML 文件读取原始总配置。

    注意：此处不递归解析环境变量，也不构建所有配置段。
    具体配置段在 ConfigRegistry 对应 property 被访问时才按需解析和构建。
    """

    config_path = Path(yaml_file_path)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件: {yaml_file_path}")

    _load_dot_env(config_path, env_file_path)

    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config_data, dict):
        raise ValueError("配置文件内容必须是对象结构。")
    return config_data


def _build_runtime_config(data: dict[str, Any]) -> AgentRuntimeServiceConfig:
    """从 YAML 字典构建宿主运行时服务配置。"""
    return AgentRuntimeServiceConfig(
        service_instance_name=str(data["service_instance_name"]),
        service_environment=str(data["service_environment"]),
        service_host=str(data["service_host"]),
        service_port=int(data["service_port"]),
    )


def _build_resource_config(data: dict[str, Any]) -> FileResourceServiceConfig:
    """从 YAML 字典构建统一文件资源服务配置，并校验对象存储提供商一致性。"""
    oss_data = _require_section(data, "object_storage_gateway")
    config = FileResourceServiceConfig(
        file_resource_batch_limit=int(data["file_resource_batch_limit"]),
        local_resource_storage_root_path=str(data["local_resource_storage_root_path"]),
        object_storage_gateway_provider=str(data["object_storage_gateway_provider"]),
        object_storage_gateway=_build_oss_config(oss_data),
    )
    if config.object_storage_gateway_provider != config.object_storage_gateway.gateway_provider:
        raise ValueError(
            "统一文件资源服务配置中的 object_storage_gateway_provider "
            "与 object_storage_gateway.gateway_provider 必须保持一致。"
        )
    return config


def _build_oss_config(data: dict[str, Any]) -> ObjectStorageGatewayConfig:
    """从 YAML 字典构建对象存储南向网关配置。"""
    return ObjectStorageGatewayConfig(
        gateway_provider=str(data["gateway_provider"]),
        object_storage_endpoint=str(data["object_storage_endpoint"]),
        object_storage_region=str(data["object_storage_region"]),
        object_storage_bucket_name=str(data["object_storage_bucket_name"]),
        object_storage_public_base_url=str(data["object_storage_public_base_url"]),
        object_storage_object_key_prefix=str(data["object_storage_object_key_prefix"]),
        object_storage_access_key_id=str(data["object_storage_access_key_id"]),
        object_storage_access_key_secret=str(data["object_storage_access_key_secret"]),
        connect_timeout_seconds=int(data.get("connect_timeout_seconds", 60)),
    )


def _build_prompt_template_config(data: dict[str, Any]) -> PromptTemplateConfig:
    """从 YAML 字典构建提示词模板服务配置。"""
    return PromptTemplateConfig(
        snippets_dir=str(data["snippets_dir"]),
    )


def _build_skill_config(data: dict[str, Any]) -> SkillConfig:
    """从 YAML 字典构建 Skill 服务配置。"""
    return SkillConfig(
        skills_dir=str(data["skills_dir"]),
    )


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


def _build_conv_repo_config(data: dict[str, Any]) -> ConversationRepositoryConfig:
    """从 YAML 字典构建会话消息持久化仓储配置。"""
    return ConversationRepositoryConfig(
        data_dir=str(data["data_dir"]),
    )


# ── .env 加载 + 环境变量解析 ─────────────────────────────────


def _load_dot_env(yaml_config_path: Path, env_file_path: str | None) -> None:
    """加载 .env 文件中的环境变量。

    优先使用显式指定的路径，否则自动查找 YAML 文件同目录下的 .env。
    使用 python-dotenv（如已安装），否则用内置简易解析兜底。
    .env 文件不存在时静默跳过。不覆盖已有的系统环境变量。
    """
    if env_file_path is not None:
        dot_env_path = Path(env_file_path)
    else:
        dot_env_path = yaml_config_path.parent / ".env"

    if not dot_env_path.exists():
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(dot_env_path, override=False)
    except ImportError:
        for line in dot_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


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
        return os.path.expanduser(resolved)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _require_section(root: dict[str, Any], section_name: str) -> dict[str, Any]:
    """要求指定配置段必须存在且为对象结构。"""
    section = root.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段 {section_name} 必须存在且必须是对象结构。")
    return section
