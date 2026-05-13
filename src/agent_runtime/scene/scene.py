# -*- coding: utf-8 -*-
"""Scene 领域聚合根。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Final, TypeAlias

SceneStatus: TypeAlias = str

# 场景处于活跃可用状态，允许在其下创建新的 AgentTask。
SCENE_STATUS_ACTIVE: Final[str] = "active"

# 场景已归档；保留消息历史供查阅，但不允许在其下创建新的 AgentTask。
SCENE_STATUS_ARCHIVED: Final[str] = "archived"

SCENE_STATUSES: Final[frozenset[str]] = frozenset({
    SCENE_STATUS_ACTIVE,
    SCENE_STATUS_ARCHIVED,
})


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
class Scene:
    """场景聚合根。

    场景是项目内的一个长期任务上下文，跨多个 AgentTask 共享同一通信流（stream-scene-{scene_id}），
    前端 tab 与 scene 一一对应，刷新页面后通过 scene_id 重连即可恢复完整对话历史。

    业务规则：
    - scene_id 全局唯一；非空且无首尾空白。
    - project_id 必须存在；引用关系由领域服务层校验，仓储层不强制外键以便回填灵活。
    - status 仅限 'active' / 'archived'；archived 场景不允许创建新 task（领域服务拒绝）。
    - bound_agent_definition_id 永久绑定主 Agent 定义；scene 创建时必填且**不可修改**，
      Scene 的长期 root agent instance 始终使用同一 agent 定义；变更 Agent 等价于新建 scene。
    - bound_root_conversation_id 是旧版本跨 root instance 重建时复用会话的兼容字段。
      新业务规则下 conversation_id 按 task 创建和清理，不再作为 Scene 跨 task 上下文复用依据。
    - LLM 策略不与 scene 绑定，只属于 agent 实例（见 AgentInstance.llm_*）。Scene 长期 root instance
      会保留运行中切换后的 LLM 策略，后续 task 复用同一 root instance 时继续沿用该策略。
    - 状态流转：active <-> archived 可双向切换；不允许直接删除（删除是仓储级硬删除）。
    """

    # 场景唯一标识。
    scene_id: str

    # 所属项目 ID。
    project_id: str

    # 场景可读名。
    name: str

    # 场景长描述（用户自定义任务上下文摘要）。
    description: str

    # 场景当前状态：active / archived。
    status: SceneStatus

    # 永久绑定的主 Agent 定义 ID。创建后不可修改。
    bound_agent_definition_id: str

    # 创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次活动时间（发送消息、收到事件、状态切换等都会更新）。前端按此倒序排列。
    last_active_at: datetime = None  # type: ignore[assignment]

    # 旧版本 scene 级对话上下文 ID。当前业务要求每个 task 创建独立 conversation_id，
    # 因此该字段仅作为历史兼容数据保留，新 task 不再读取它作为会话复用依据。
    # None 表示该 scene 从未写入过旧版 scene 级 conversation。
    bound_root_conversation_id: str | None = None

    # 预留扩展字段。
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验场景基础字段。"""
        _require_non_blank(self.scene_id, "scene_id")
        _require_non_blank(self.project_id, "project_id")
        _require_non_blank(self.name, "name")
        _require_non_blank(self.bound_agent_definition_id, "bound_agent_definition_id")
        # description 允许空字符串（用户没填具体描述也可以创建场景），但不允许 None；
        # 上层若传入 None 应在网关层归一化为空串。
        if self.description is None:
            raise ValueError("description 不能为 None，可以是空字符串但不能省略。")
        if self.status not in SCENE_STATUSES:
            raise ValueError(f"未知 scene 状态: {self.status}")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.last_active_at is None:
            object.__setattr__(self, "last_active_at", self.created_at)

    @property
    def is_archived(self) -> bool:
        """场景是否已归档。"""
        return self.status == SCENE_STATUS_ARCHIVED

    def touch_active(self, *, at: datetime | None = None) -> "Scene":
        """更新 last_active_at 时间戳，返回新副本。

        发送消息、Agent 事件落盘、状态切换等动作触发本方法，用于按最近活动倒序展示场景列表。
        """
        return replace(self, last_active_at=at or _utc_now())

    def archive(self, *, at: datetime | None = None) -> "Scene":
        """归档场景，返回新副本。归档后不允许在该场景下创建新 task。"""
        if self.status == SCENE_STATUS_ARCHIVED:
            return self
        return replace(
            self,
            status=SCENE_STATUS_ARCHIVED,
            last_active_at=at or _utc_now(),
        )

    def activate(self, *, at: datetime | None = None) -> "Scene":
        """重新激活场景，返回新副本。"""
        if self.status == SCENE_STATUS_ACTIVE:
            return self
        return replace(
            self,
            status=SCENE_STATUS_ACTIVE,
            last_active_at=at or _utc_now(),
        )

    def update_metadata(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Scene":
        """更新场景的可变元数据，返回新副本。

        bound_agent_definition_id 永久绑定不可修改；如需更换 Agent 请新建 scene。
        bound_root_conversation_id 是历史兼容字段，不再作为新 task 的 conversation 复用依据，
        也不属于用户可改的元数据。所有参数都是 None 表示不变。
        """
        values: dict[str, Any] = {}
        if name is not None:
            _require_non_blank(name, "name")
            values["name"] = name
        if description is not None:
            values["description"] = description
        if metadata is not None:
            values["metadata"] = dict(metadata)
        if not values:
            return self
        return replace(self, **values)

    def with_bound_root_conversation_id(self, conversation_id: str) -> "Scene":
        """回填旧版 scene 级 bound_root_conversation_id，返回新副本。

        该字段保留给已写入 scene 级 conversation 的历史数据兼容。新业务规则要求
        每个 task 独立创建 conversation_id，并在 end_task 后从 root agent instance 清空，
        因此新编排流程不应再依赖该字段实现跨 task 会话复用。

        Raises:
            ValueError: conversation_id 为空或当前 scene 已绑定其它 conversation_id。
        """
        _require_non_blank(conversation_id, "conversation_id")
        if self.bound_root_conversation_id is not None and self.bound_root_conversation_id != conversation_id:
            raise ValueError(
                f"scene {self.scene_id} 已绑定 conversation {self.bound_root_conversation_id}，"
                f"不能再绑定到 {conversation_id}。"
            )
        if self.bound_root_conversation_id == conversation_id:
            return self
        return replace(self, bound_root_conversation_id=conversation_id)
