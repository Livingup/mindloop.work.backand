# -*- coding: utf-8 -*-
"""Project 领域聚合根。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _require_non_blank(value: str, field_name: str) -> None:
    """校验字符串字段不能为空且无首尾空白。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


@dataclass(frozen=True, slots=True)
class Project:
    """项目聚合根。

    一个项目对应一个项目目录（workspace 根，通常是 git 仓库根）。一个项目下可以创建
    任意数量的场景（Scene），每个场景是该项目内的一个长期任务上下文。

    业务规则：
    - project_id 全局唯一；非空且无首尾空白。
    - workspace_path 必须是绝对路径（保证后端可以无歧义访问）。
    - name 默认从 workspace_path 的 basename 派生；允许用户自定义可读名。
    - 同一 workspace_path 在仓储层强制唯一（不允许重复注册同一目录的两个项目）。
    """

    # 项目唯一标识。
    project_id: str

    # 项目可读名（默认从 workspace_path basename 派生）。
    name: str

    # 项目目录绝对路径。所有场景下属于本项目的 task 都以该路径作为 current_working_directory。
    workspace_path: str

    # 创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次"打开"项目的时间。前端列项目时按此倒序。
    last_opened_at: datetime = None  # type: ignore[assignment]

    # 预留扩展字段（图标、颜色、用户备注等）。
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验项目基础字段。"""
        _require_non_blank(self.project_id, "project_id")
        _require_non_blank(self.name, "name")
        _require_non_blank(self.workspace_path, "workspace_path")
        # workspace_path 必须是绝对路径；Windows 用 "X:\..." 或 "\\..." 视为绝对。
        if not _is_absolute_path(self.workspace_path):
            raise ValueError(f"workspace_path 必须是绝对路径: {self.workspace_path}")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.last_opened_at is None:
            object.__setattr__(self, "last_opened_at", self.created_at)

    def touch_opened(self, *, at: datetime | None = None) -> "Project":
        """更新 last_opened_at 时间戳，返回新副本。

        前端"打开项目"动作触发本方法，用于按最近打开排序项目列表。
        """
        return replace(self, last_opened_at=at or _utc_now())

    def rename(self, new_name: str) -> "Project":
        """重命名项目，返回新副本。"""
        _require_non_blank(new_name, "name")
        return replace(self, name=new_name)


def _is_absolute_path(path: str) -> bool:
    """判断给定字符串是否表示绝对路径（兼容 Windows 与 POSIX 风格）。"""
    if not path:
        return False
    # POSIX 绝对路径以 / 开头。
    if path.startswith("/"):
        return True
    # Windows UNC 路径 \\server\share 或 \\?\X:\...
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    # Windows 盘符路径 X:\... 或 X:/...
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
        return True
    return False
