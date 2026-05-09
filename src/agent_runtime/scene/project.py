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

    一个项目对应 workspace 目录下 `.spread-ai/project.json` 的一份 manifest 文件。
    project_id 是项目的永久身份凭证，由 manifest 写入目录后跟随目录移动/重命名而不丢失，
    所有与项目绑定的数据（scenes、conversations 等）都通过 project_id 关联。

    workspace_path **不属于** 项目聚合状态，只作为运行时入参用于定位 manifest 的物理位置；
    同一 project_id 在不同时间可以对应不同的 workspace_path（用户搬迁目录），但业务关系不变。

    业务规则：
    - project_id 全局唯一；非空；创建时由 uuid4 生成并写入 manifest，后续永久不变。
    - name 人类可读项目名，默认从 workspace basename 派生，允许用户后续重命名。
    - created_at / last_opened_at 为项目生命周期时间戳，由 manifest 读写维护。
    - metadata 预留用户扩展字段（图标、颜色、备注等）。
    """

    # 项目永久唯一标识，形如 "proj-<uuid4_hex_24>"。
    project_id: str

    # 项目可读名。
    name: str

    # 创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次"打开"项目的时间。前端列项目时按此倒序。
    last_opened_at: datetime = None  # type: ignore[assignment]

    # 预留扩展字段。
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验项目基础字段。"""
        _require_non_blank(self.project_id, "project_id")
        _require_non_blank(self.name, "name")
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

    def with_metadata(self, metadata: dict[str, Any]) -> "Project":
        """全量替换 metadata，返回新副本。"""
        return replace(self, metadata=dict(metadata))
