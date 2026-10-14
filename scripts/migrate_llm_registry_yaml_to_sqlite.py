# -*- coding: utf-8 -*-
"""一次性将 legacy LLM Registry YAML 数据导入 SQLite。

该脚本是本迭代迁移工具，不属于应用运行期业务链路：应用启动和 SQLite 仓储初始化不会自动读取
YAML。执行前请确认目标 registry_dir，执行后运行期以 llm_model_registry.sqlite3 为权威数据源。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from agent_runtime.llm_model_registry.infrastructure.sqlite_llm_model_registry_repository import (
    SqliteLlmModelRegistryRepository,
    llm_model_registry_db_path,
)
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_service import LlmModelRegistryService
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition


@dataclass(frozen=True, slots=True)
class MigrationSummary:
    """LLM Registry legacy YAML 导入结果摘要。"""

    imported_models: int = 0
    skipped_models: int = 0
    failed_models: int = 0
    imported_providers: int = 0
    skipped_providers: int = 0
    failed_providers: int = 0
    imported_policies: int = 0
    skipped_policies: int = 0
    failed_policies: int = 0

    @property
    def imported_total(self) -> int:
        return self.imported_models + self.imported_providers + self.imported_policies

    @property
    def skipped_total(self) -> int:
        return self.skipped_models + self.skipped_providers + self.skipped_policies

    @property
    def failed_total(self) -> int:
        return self.failed_models + self.failed_providers + self.failed_policies


def migrate_registry_yaml_to_sqlite(registry_dir: str | Path, *, overwrite: bool = False) -> MigrationSummary:
    """把指定 registry_dir 下的 legacy YAML 导入同目录 SQLite DB。

    Args:
        registry_dir: 旧 LLM Registry 根目录，包含 models/providers/execution-policies 子目录。
        overwrite: 为 True 时 YAML 覆盖 DB 中同 ID 记录；默认 False 表示 DB 已存在则跳过。

    Returns:
        迁移摘要。
    """
    root = Path(registry_dir).expanduser()
    repository = SqliteLlmModelRegistryRepository(lambda: str(root))
    repository.ensure_initialized()
    service = LlmModelRegistryService(repository)
    try:
        model_stats = _migrate_models(root, repository, service, overwrite=overwrite)
        provider_stats = _migrate_providers(root, repository, service, overwrite=overwrite)
        policy_stats = _migrate_policies(root, repository, service, overwrite=overwrite)
        return MigrationSummary(
            imported_models=model_stats.imported,
            skipped_models=model_stats.skipped,
            failed_models=model_stats.failed,
            imported_providers=provider_stats.imported,
            skipped_providers=provider_stats.skipped,
            failed_providers=provider_stats.failed,
            imported_policies=policy_stats.imported,
            skipped_policies=policy_stats.skipped,
            failed_policies=policy_stats.failed,
        )
    finally:
        repository.close()


@dataclass(slots=True)
class _Stats:
    imported: int = 0
    skipped: int = 0
    failed: int = 0


def _migrate_models(
    root: Path,
    repository: SqliteLlmModelRegistryRepository,
    service: LlmModelRegistryService,
    *,
    overwrite: bool,
) -> _Stats:
    stats = _Stats()
    for yaml_file in _iter_yaml_files(root / "models"):
        model_id = yaml_file.stem
        try:
            metadata = LlmModelMetadata.from_dict(model_id, _read_yaml_object(yaml_file))
            if metadata.model_id != model_id:
                raise ValueError(f"文件名 ID '{model_id}' 与 YAML 内 model_id '{metadata.model_id}' 不一致。")
            if not overwrite and repository.contains_model_metadata(metadata.model_id):
                stats.skipped += 1
                _print_skip("model", metadata.model_id)
                continue
            service.save_model_metadata(metadata)
            stats.imported += 1
            _print_imported("model", metadata.model_id)
        except Exception as exc:
            stats.failed += 1
            _print_failed("model", model_id, exc)
    return stats


def _migrate_providers(
    root: Path,
    repository: SqliteLlmModelRegistryRepository,
    service: LlmModelRegistryService,
    *,
    overwrite: bool,
) -> _Stats:
    stats = _Stats()
    for yaml_file in _iter_yaml_files(root / "providers"):
        provider_id = yaml_file.stem
        try:
            provider = LlmProviderDefinition.from_dict(provider_id, _read_yaml_object(yaml_file))
            if provider.provider_id != provider_id:
                raise ValueError(f"文件名 ID '{provider_id}' 与 YAML 内 provider_id '{provider.provider_id}' 不一致。")
            if not overwrite and repository.contains_provider(provider.provider_id):
                stats.skipped += 1
                _print_skip("provider", provider.provider_id)
                continue
            service.save_provider(provider)
            stats.imported += 1
            _print_imported("provider", provider.provider_id)
        except Exception as exc:
            stats.failed += 1
            _print_failed("provider", provider_id, exc)
    return stats


def _migrate_policies(
    root: Path,
    repository: SqliteLlmModelRegistryRepository,
    service: LlmModelRegistryService,
    *,
    overwrite: bool,
) -> _Stats:
    stats = _Stats()
    for yaml_file in _iter_yaml_files(root / "execution-policies"):
        policy_group_id = yaml_file.stem
        try:
            policy = LlmExecutionPolicy.from_dict(policy_group_id, _read_yaml_object(yaml_file))
            if not overwrite and repository.contains_execution_policy(policy.policy_group_id):
                stats.skipped += 1
                _print_skip("policy", policy.policy_group_id)
                continue
            service.save_execution_policy(policy)
            stats.imported += 1
            _print_imported("policy", policy.policy_group_id)
        except Exception as exc:
            stats.failed += 1
            _print_failed("policy", policy_group_id, exc)
    return stats


def _iter_yaml_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.yaml"))


def _read_yaml_object(yaml_file: Path) -> dict[str, Any]:
    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("YAML 文件内容必须是对象结构。")
    return data


def _print_imported(kind: str, item_id: str) -> None:
    print(f"[imported] {kind}: {item_id}")


def _print_skip(kind: str, item_id: str) -> None:
    print(f"[skipped] {kind}: {item_id} 已存在于 SQLite，未覆盖。")


def _print_failed(kind: str, item_id: str, exc: Exception) -> None:
    print(f"[failed] {kind}: {item_id} 导入失败: {exc}", file=sys.stderr)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="一次性将 legacy LLM Registry YAML 数据导入同目录 SQLite DB。",
    )
    parser.add_argument(
        "--registry-dir",
        required=True,
        help="LLM Registry 根目录，例如 ~/.spread-ai/llm-model-registry。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许 YAML 覆盖 SQLite 中已存在的同 ID 数据；默认跳过已存在数据。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    registry_dir = Path(args.registry_dir).expanduser()
    db_path = llm_model_registry_db_path(str(registry_dir))
    print(f"[start] registry_dir={registry_dir}")
    print(f"[start] sqlite_db={db_path}")
    summary = migrate_registry_yaml_to_sqlite(registry_dir, overwrite=args.overwrite)
    print(
        "[summary] "
        f"imported={summary.imported_total} "
        f"skipped={summary.skipped_total} "
        f"failed={summary.failed_total} "
        f"models={summary.imported_models}/{summary.skipped_models}/{summary.failed_models} "
        f"providers={summary.imported_providers}/{summary.skipped_providers}/{summary.failed_providers} "
        f"policies={summary.imported_policies}/{summary.skipped_policies}/{summary.failed_policies}"
    )
    if summary.failed_total:
        return 1
    print("[done] 迁移完成。运行期将使用 SQLite DB；legacy YAML 不再是权威数据源。")
    print("[notice] legacy YAML 文件未自动删除；其中可能仍包含明文 API Key，请在确认 DB 正常后自行归档或删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
