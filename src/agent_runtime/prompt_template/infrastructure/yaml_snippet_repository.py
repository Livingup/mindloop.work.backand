"""基于 YAML 文件的提示词片段仓储实现。

片段以独立 YAML 文件存储，文件名使用 UUID。
启动时扫描存储目录，自动建立 name → 文件路径 的内存索引。
支持子目录分类组织，扫描递归遍历。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import yaml

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet

logger = logging.getLogger(__name__)


class YamlFileSnippetRepository:
    """基于 YAML 文件的提示词片段仓储实现。"""

    def __init__(self, snippets_dir: str | Path) -> None:
        self._snippets_dir = Path(snippets_dir)
        # name → YAML 文件绝对路径的内存索引。
        self._index: dict[str, Path] = {}
        self._snippets_dir.mkdir(parents=True, exist_ok=True)
        self._scan()

    def find_by_name(self, name: str) -> PromptSnippet | None:
        """根据唯一描述名查找片段。未找到时返回 None。"""
        path = self._index.get(name)
        if path is None:
            return None
        return self._read_snippet(path)

    def find_all(self) -> list[PromptSnippet]:
        """返回所有已持久化的片段。"""
        return [self._read_snippet(p) for p in self._index.values()]

    def save(self, snippet: PromptSnippet) -> None:
        """保存片段。name 已存在时覆写，不存在时新增。

        新增时根据 category 决定存储子目录；覆写时如果 category 变更则迁移文件。
        """
        existing_path = self._index.get(snippet.name)
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

        self._write_yaml(path, snippet)
        self._index[snippet.name] = path

    def delete(self, name: str) -> bool:
        """删除片段。返回是否实际删除了。"""
        path = self._index.pop(name, None)
        if path is not None and path.exists():
            path.unlink()
            return True
        return False

    def exists(self, name: str) -> bool:
        """判断指定 name 的片段是否存在。"""
        return name in self._index

    def rename(self, old_name: str, new_name: str) -> None:
        """更新索引中的 name 映射（文件不变，只改索引键）。"""
        path = self._index.pop(old_name, None)
        if path is not None:
            self._index[new_name] = path

    # ── 内部方法 ─────────────────────────────────────────────────

    def _scan(self) -> None:
        """扫描存储目录，建立 name → 文件路径 的内存索引。

        递归遍历所有子目录中的 .yaml 文件，读取 name 字段注册到索引。
        name 重复时记录警告并跳过后发现的文件。
        """
        self._index.clear()
        for path in self._snippets_dir.rglob("*.yaml"):
            try:
                data = self._read_yaml(path)
            except Exception:
                logger.warning("跳过无法解析的片段文件: %s", path, exc_info=True)
                continue

            name = data.get("name")
            if not name or not isinstance(name, str):
                logger.warning("跳过缺少 name 字段的片段文件: %s", path)
                continue

            if name in self._index:
                logger.warning(
                    "name 重复，跳过: '%s' (已注册文件: %s, 冲突文件: %s)",
                    name, self._index[name], path,
                )
                continue

            self._index[name] = path

        logger.info("提示词片段索引扫描完成，共注册 %d 个片段。", len(self._index))

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        """读取并解析 YAML 文件。"""
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _read_snippet(self, path: Path) -> PromptSnippet:
        """从 YAML 文件读取提示词片段。"""
        data = self._read_yaml(path)
        # category 优先从文件内容读取，回退到目录结构推断，最终回退到 "default"。
        category = data.get("category")
        if not category and path.parent != self._snippets_dir:
            category = path.parent.relative_to(self._snippets_dir).as_posix()
        return PromptSnippet(
            name=data["name"],
            text=data.get("text", ""),
            category=category or "default",
        )

    def _write_yaml(self, path: Path, snippet: PromptSnippet) -> None:
        """将提示词片段写入 YAML 文件。"""
        data: dict[str, Any] = {
            "name": snippet.name,
            "text": snippet.text,
            "category": snippet.category,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
