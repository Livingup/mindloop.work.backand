# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册 YAML 文件仓储实现。

用户自定义注册数据按类型拆分为独立 YAML 文件：
  ~/.spread-ai/llm-model-registry/models/{model_id}.yaml
  ~/.spread-ai/llm-model-registry/providers/{provider_id}.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition
from agent_runtime.llm_model_registry.llm_registry_validation import validate_registry_id


class YamlLlmModelRegistryRepository:
    """基于 YAML 文件的 LLM 模型与服务商注册仓储。

    仓储不缓存 registry_dir 配置值；每次访问目录都通过 accessor 从 ConfigRegistry 实时读取，
    确保配置热更新后模型元数据和服务商定义可以切换到新的用户数据目录。
    """

    def __init__(self, registry_dir_accessor: Callable[[], str]) -> None:
        """初始化仓储并确保模型与服务商目录存在。

        Args:
            registry_dir_accessor: 注册数据根目录访问器，每次调用时实时读取配置中心数据。
        """
        # 注册数据目录访问器。仓储不保存配置值副本，避免 ConfigRegistry 热更新后读到旧目录。
        self._registry_dir_accessor = registry_dir_accessor
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._providers_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _registry_dir(self) -> Path:
        """实时读取 LLM 注册数据根目录路径。"""
        return Path(self._registry_dir_accessor())

    @property
    def _models_dir(self) -> Path:
        """模型元数据 YAML 文件目录。"""
        return self._registry_dir / "models"

    @property
    def _providers_dir(self) -> Path:
        """服务商定义 YAML 文件目录。"""
        return self._registry_dir / "providers"

    # ── 模型元数据集合 ──────────────────────────────────────

    def find_all_model_metadata(self) -> list[LlmModelMetadata]:
        """列出所有模型元数据。

        扫描 models 目录下所有 .yaml 文件，逐个解析为 LlmModelMetadata。
        单个文件解析失败时跳过，避免一个损坏模型配置阻断其它配置查询。
        """
        metadata_list: list[LlmModelMetadata] = []
        if not self._models_dir.exists():
            return metadata_list
        for yaml_file in sorted(self._models_dir.glob("*.yaml")):
            metadata = self._load_model_file(yaml_file.stem, yaml_file)
            if metadata is not None:
                metadata_list.append(metadata)
        return metadata_list

    def find_model_metadata_by_id(self, model_id: str) -> LlmModelMetadata | None:
        """按 model_id 查找模型元数据。"""
        yaml_file = self._get_model_file_path(model_id)
        if not yaml_file.exists():
            return None
        return self._load_model_file(model_id, yaml_file)

    def save_model_metadata(self, metadata: LlmModelMetadata) -> None:
        """保存模型元数据到 YAML 文件。"""
        self._models_dir.mkdir(parents=True, exist_ok=True)
        yaml_file = self._get_model_file_path(metadata.model_id)
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(
                metadata.to_dict(),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def delete_model_metadata(self, model_id: str) -> bool:
        """删除模型元数据文件。"""
        yaml_file = self._get_model_file_path(model_id)
        if yaml_file.exists():
            yaml_file.unlink()
            return True
        return False

    def contains_model_metadata(self, model_id: str) -> bool:
        """判断模型元数据文件是否存在。"""
        return self._get_model_file_path(model_id).exists()

    # ── 服务商定义集合 ──────────────────────────────────────

    def find_all_providers(self) -> list[LlmProviderDefinition]:
        """列出所有 LLM 服务商定义。"""
        providers: list[LlmProviderDefinition] = []
        if not self._providers_dir.exists():
            return providers
        for yaml_file in sorted(self._providers_dir.glob("*.yaml")):
            provider = self._load_provider_file(yaml_file.stem, yaml_file)
            if provider is not None:
                providers.append(provider)
        return providers

    def find_provider_by_id(self, provider_id: str) -> LlmProviderDefinition | None:
        """按 provider_id 查找服务商定义。"""
        yaml_file = self._get_provider_file_path(provider_id)
        if not yaml_file.exists():
            return None
        return self._load_provider_file(provider_id, yaml_file)

    def save_provider(self, provider: LlmProviderDefinition) -> None:
        """保存服务商定义到 YAML 文件。"""
        self._providers_dir.mkdir(parents=True, exist_ok=True)
        yaml_file = self._get_provider_file_path(provider.provider_id)
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(
                provider.to_dict(),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def delete_provider(self, provider_id: str) -> bool:
        """删除服务商定义文件。"""
        yaml_file = self._get_provider_file_path(provider_id)
        if yaml_file.exists():
            yaml_file.unlink()
            return True
        return False

    def contains_provider(self, provider_id: str) -> bool:
        """判断服务商定义文件是否存在。"""
        return self._get_provider_file_path(provider_id).exists()

    # ── 内部方法 ──────────────────────────────────────────────

    def _get_model_file_path(self, model_id: str) -> Path:
        """获取模型元数据文件路径，并执行文件名安全校验。"""
        validate_registry_id(model_id, "model_id")
        return self._models_dir / f"{model_id}.yaml"

    def _get_provider_file_path(self, provider_id: str) -> Path:
        """获取服务商定义文件路径，并执行文件名安全校验。"""
        validate_registry_id(provider_id, "provider_id")
        return self._providers_dir / f"{provider_id}.yaml"

    def _load_model_file(
        self,
        model_id: str,
        yaml_file: Path,
    ) -> LlmModelMetadata | None:
        """从 YAML 文件加载模型元数据。

        解析失败时返回 None；调用方按场景决定是否把 None 转换为未找到或跳过。
        """
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            metadata = LlmModelMetadata.from_dict(model_id, data)
            if metadata.model_id != model_id:
                return None
            return metadata
        except Exception:
            return None

    def _load_provider_file(
        self,
        provider_id: str,
        yaml_file: Path,
    ) -> LlmProviderDefinition | None:
        """从 YAML 文件加载服务商定义。"""
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            provider = LlmProviderDefinition.from_dict(provider_id, data)
            if provider.provider_id != provider_id:
                return None
            return provider
        except Exception:
            return None
