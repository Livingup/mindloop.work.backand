"""统一文件资源服务配置模型与 YAML 加载器。

该模块集中定义统一文件资源服务运行所需的全部配置模型，
以及从 YAML 文件加载配置的工具。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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


@dataclass(frozen=True, slots=True)
class ObjectStorageGatewayConfig:
    """对象存储南向网关配置。"""

    # 对象存储网关提供商标识（memory / aliyun_oss）。
    gateway_provider: str

    # 对象存储服务 endpoint。
    object_storage_endpoint: str

    # 对象存储区域。
    object_storage_region: str

    # 对象存储桶名称。
    object_storage_bucket_name: str

    # 对象存储公共访问地址前缀。
    object_storage_public_base_url: str

    # 对象存储对象键前缀。
    object_storage_object_key_prefix: str

    # 对象存储访问账号 ID。
    object_storage_access_key_id: str

    # 对象存储访问账号密钥。
    object_storage_access_key_secret: str

    # 对象存储连接超时时间，单位秒。默认 60 秒。
    connect_timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class FileResourceServiceConfig:
    """统一文件资源服务配置。"""

    # 单次批量处理上限。
    file_resource_batch_limit: int

    # 本地资源热存储根目录。
    local_resource_storage_root_path: str

    # 当前启用的对象存储网关提供商。
    object_storage_gateway_provider: str

    # 对象存储南向网关配置。
    object_storage_gateway: ObjectStorageGatewayConfig


@dataclass(frozen=True, slots=True)
class AgentRuntimeYamlConfig:
    """通用 Agent 运行时 YAML 总配置。"""

    # 通用 Agent 运行时宿主服务配置。
    agent_runtime_service: AgentRuntimeServiceConfig

    # 统一文件资源服务配置。
    file_resource_service: FileResourceServiceConfig


class AgentRuntimeYamlConfigLoader:
    """通用 Agent 运行时 YAML 配置加载器。

    从 YAML 文件读取运行配置，转换为结构化配置模型。
    """

    # 匹配 ${ENV_VAR} 或 ${ENV_VAR:-default} 格式的环境变量占位符。
    _ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

    def load_from_yaml_file(
        self,
        yaml_file_path: str,
        env_file_path: str | None = None,
    ) -> AgentRuntimeYamlConfig:
        """从 YAML 配置文件加载通用 Agent 运行时总配置。

        YAML 中的字符串值支持 ${ENV_VAR} 占位符语法，加载时自动从环境变量解析。
        支持 ${ENV_VAR:-default} 指定缺省值。若未指定缺省值且环境变量不存在则报错。

        如果提供了 env_file_path，会先从该 .env 文件加载环境变量。
        如果未提供，会自动查找 YAML 文件同目录下的 .env 文件。
        """

        config_path = Path(yaml_file_path)
        if not config_path.exists():
            raise FileNotFoundError(f"未找到通用 Agent 运行时 YAML 配置文件: {yaml_file_path}")

        # 加载 .env 文件中的环境变量（不覆盖已有系统环境变量）。
        self._load_dot_env(config_path, env_file_path)

        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config_data, dict):
            raise ValueError("通用 Agent 运行时 YAML 配置文件内容必须是对象结构。")

        # 递归解析所有字符串值中的 ${ENV_VAR} 占位符。
        config_data = self._resolve_env_vars(config_data)

        runtime_data = self._require_section(config_data, "agent_runtime_service")
        resource_data = self._require_section(config_data, "file_resource_service")

        return AgentRuntimeYamlConfig(
            agent_runtime_service=self._build_runtime_config(runtime_data),
            file_resource_service=self._build_resource_config(resource_data),
        )

    def _build_runtime_config(
        self,
        data: dict[str, Any],
    ) -> AgentRuntimeServiceConfig:
        """构建宿主运行时服务配置。"""

        return AgentRuntimeServiceConfig(
            service_instance_name=str(data["service_instance_name"]),
            service_environment=str(data["service_environment"]),
            service_host=str(data["service_host"]),
            service_port=int(data["service_port"]),
        )

    def _build_resource_config(
        self,
        data: dict[str, Any],
    ) -> FileResourceServiceConfig:
        """构建统一文件资源服务配置。"""

        oss_data = self._require_section(data, "object_storage_gateway")
        config = FileResourceServiceConfig(
            file_resource_batch_limit=int(data["file_resource_batch_limit"]),
            local_resource_storage_root_path=str(data["local_resource_storage_root_path"]),
            object_storage_gateway_provider=str(data["object_storage_gateway_provider"]),
            object_storage_gateway=self._build_oss_config(oss_data),
        )
        self._assert_provider_consistency(config)
        return config

    def _build_oss_config(
        self,
        data: dict[str, Any],
    ) -> ObjectStorageGatewayConfig:
        """构建对象存储网关配置。"""

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

    def _load_dot_env(
        self,
        yaml_config_path: Path,
        env_file_path: str | None,
    ) -> None:
        """加载 .env 文件到环境变量。

        优先使用显式指定的路径，否则自动查找 YAML 配置文件同目录下的 .env。
        使用 python-dotenv（如果已安装），否则用内置简易解析。
        .env 文件不存在时静默跳过。
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
            # python-dotenv 未安装时，用简易解析兜底。
            self._load_dot_env_fallback(dot_env_path)

    def _load_dot_env_fallback(self, dot_env_path: Path) -> None:
        """简易 .env 文件解析。支持 KEY=VALUE 格式，忽略注释和空行。"""

        for line in dot_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # 不覆盖已有的系统环境变量。
            if key not in os.environ:
                os.environ[key] = value

    def _resolve_env_vars(self, value: Any) -> Any:
        """递归解析配置值中的 ${ENV_VAR} 占位符。

        支持格式：
        - ${ENV_VAR}         — 必须存在，不存在则报错
        - ${ENV_VAR:-default} — 不存在时使用 default
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
            return self._ENV_VAR_PATTERN.sub(_replace, value)
        if isinstance(value, dict):
            return {k: self._resolve_env_vars(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_env_vars(item) for item in value]
        return value

    def _require_section(
        self,
        root: dict[str, Any],
        section_name: str,
    ) -> dict[str, Any]:
        """要求指定配置段必须存在且为对象结构。"""

        section = root.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(f"配置段 {section_name} 必须存在且必须是对象结构。")
        return section

    def _assert_provider_consistency(
        self,
        config: FileResourceServiceConfig,
    ) -> None:
        """校验对象存储提供商声明一致性。"""

        if config.object_storage_gateway_provider != config.object_storage_gateway.gateway_provider:
            raise ValueError(
                "统一文件资源服务配置中的 object_storage_gateway_provider "
                "与 object_storage_gateway.gateway_provider 必须保持一致。"
            )
