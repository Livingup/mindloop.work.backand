"""基于 YAML 文件的提示词片段仓储实现。

设计原则：
- 内存中持有完整的聚合根集合（PromptSnippet 实例），作为读操作的主要数据源。
- 所有写操作先更新内存中的聚合根集合，再同步持久化到 YAML 文件。
- 启动时从文件系统加载所有聚合根到内存（冷启动恢复）。
- 读操作前自动检测文件系统变更（目录 mtime），有外部修改时自动 reload，
  解决 CLI 与 REST 跨进程数据不一致问题。

片段以独立 YAML 文件存储，文件名使用 UUID，按 category 子目录组织。
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet

logger = logging.getLogger(__name__)


class YamlFileSnippetRepository:
    """基于 YAML 文件持久化的提示词片段仓储。

    内存中维护完整的聚合根集合 + 对应的文件路径映射。
    读操作纯内存，写操作先改内存再持久化文件。
    """

    def __init__(self, snippets_dir_accessor: Callable[[], str]) -> None:
        self._snippets_dir_accessor = snippets_dir_accessor
        # 内存中的聚合根集合：name → (聚合根实例, 持久化文件路径)。
        self._store: dict[str, tuple[PromptSnippet, Path]] = {}
        # 上次加载时文件系统的最大 mtime，用于检测外部修改。
        self._last_disk_mtime: float = 0.0
        self._snippets_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    @property
    def _snippets_dir(self) -> Path:
        """实时从配置中心读取提示词片段存储根目录路径。"""
        return Path(self._snippets_dir_accessor())

    # ── 读操作（内存优先，自动检测外部变更） ──────────────────────

    def find_by_name(self, name: str) -> PromptSnippet | None:
        """根据唯一描述名查找片段。未找到时返回 None。"""
        self._sync_if_changed()
        entry = self._store.get(name)
        return entry[0] if entry is not None else None

    def find_all(self) -> list[PromptSnippet]:
        """返回所有聚合根实例。"""
        self._sync_if_changed()
        return [snippet for snippet, _ in self._store.values()]

    def exists(self, name: str) -> bool:
        """判断指定 name 的片段是否存在。"""
        self._sync_if_changed()
        return name in self._store

    # ── 写操作（先更新内存，再持久化文件） ──────────────────────

    def save(self, snippet: PromptSnippet) -> None:
        """保存聚合根。先更新内存集合，再持久化到 YAML 文件。

        name 已存在时覆写，不存在时新增。
        新增时根据 category 决定存储子目录；覆写时如果 category 变更则迁移文件。
        """
        existing = self._store.get(snippet.name)
        existing_path = existing[1] if existing is not None else None
        target_dir = self._snippets_dir / snippet.category

        if existing_path is None:
            # 新增：存到 category 对应子目录。
            path = target_dir / f"{uuid.uuid4()}.yaml"
        elif existing_path.parent != target_dir:
            # category 变更：删旧文件，在新目录创建。
            if existing_path.exists():
                existing_path.unlink()
            path = target_dir / existing_path.name
        else:
            # 同目录覆写。
            path = existing_path

        # 先更新内存。
        self._store[snippet.name] = (snippet, path)
        # 再持久化到文件。
        self._write_yaml(path, snippet)
        # 更新 mtime 快照，避免自己的写入触发不必要的 reload。
        self._last_disk_mtime = self._get_disk_mtime()

    def delete(self, name: str) -> bool:
        """删除聚合根。先从内存移除，再删除持久化文件。返回是否实际删除了。"""
        entry = self._store.pop(name, None)
        if entry is not None:
            _, path = entry
            if path.exists():
                path.unlink()
            self._last_disk_mtime = self._get_disk_mtime()
            return True
        return False

    def rename(self, old_name: str, new_name: str) -> None:
        """更新内存集合中的 name 映射（文件内容由后续 save 更新）。"""
        entry = self._store.pop(old_name, None)
        if entry is not None:
            self._store[new_name] = entry

    # ── 跨进程同步 ──────────────────────────────────────────────

    def reload(self) -> None:
        """重新从文件系统加载所有聚合根到内存。

        用于跨进程一致性场景（如 watchdog 文件监听检测到外部修改时调用）。
        """
        self._load_from_disk()

    def _sync_if_changed(self) -> None:
        """读操作前自动检测文件系统变更，有外部修改时自动 reload。

        通过比较目录及所有 .yaml 文件的最大 mtime 与上次快照来判断是否有外部写入。
        仅当检测到变更时才执行全量 reload，无变更时开销极小（仅 stat 调用）。
        """
        current_mtime = self._get_disk_mtime()
        if current_mtime > self._last_disk_mtime:
            logger.debug("检测到文件系统变更 (mtime %.3f → %.3f)，重新加载。",
                         self._last_disk_mtime, current_mtime)
            self._load_from_disk()

    def _get_disk_mtime(self) -> float:
        """获取存储目录中所有 .yaml 文件和目录本身的最大 mtime。"""
        snippets_dir = self._snippets_dir
        if not snippets_dir.exists():
            return 0.0
        max_mtime = os.path.getmtime(snippets_dir)
        for path in snippets_dir.rglob("*.yaml"):
            try:
                mt = os.path.getmtime(path)
                if mt > max_mtime:
                    max_mtime = mt
            except OSError:
                continue
        return max_mtime

    # ── 内部方法 ─────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """从文件系统扫描并加载所有聚合根到内存。

        递归遍历存储目录中的 .yaml 文件，反序列化为 PromptSnippet 聚合根，
        存入内存集合。name 重复时记录警告并跳过后发现的文件。
        加载完成后更新 mtime 快照。
        """
        self._store.clear()
        for path in self._snippets_dir.rglob("*.yaml"):
            try:
                snippet = self._deserialize(path)
            except Exception:
                logger.warning("跳过无法解析的片段文件: %s", path, exc_info=True)
                continue

            if snippet.name in self._store:
                logger.warning(
                    "name 重复，跳过: '%s' (已注册文件: %s, 冲突文件: %s)",
                    snippet.name, self._store[snippet.name][1], path,
                )
                continue

            self._store[snippet.name] = (snippet, path)

        self._last_disk_mtime = self._get_disk_mtime()
        logger.info("提示词片段加载完成，内存中共 %d 个聚合根。", len(self._store))

    def _deserialize(self, path: Path) -> PromptSnippet:
        """从 YAML 文件反序列化为聚合根实例。"""
        with open(path, "r", encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"缺少有效的 name 字段: {path}")

        # category 优先从文件内容读取，回退到目录结构推断，最终回退到 "default"。
        category = data.get("category")
        if not category and path.parent != self._snippets_dir:
            category = path.parent.relative_to(self._snippets_dir).as_posix()

        return PromptSnippet(
            name=name,
            text=data.get("text", ""),
            category=category or "default",
        )

    def _write_yaml(self, path: Path, snippet: PromptSnippet) -> None:
        """将聚合根序列化并写入 YAML 文件。"""
        data: dict[str, Any] = {
            "name": snippet.name,
            "text": snippet.text,
            "category": snippet.category,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
