# -*- coding: utf-8 -*-
"""Agent 任务与父子实例关系领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final, TypeAlias


AgentTaskStatus: TypeAlias = str

# 任务未结束。Task 只表达用户任务生命周期，不表达执行循环是否正在运行或暂停。
# created/running/paused 等历史执行态在读取时都会归一化为 unfinished。
AGENT_TASK_STATUS_UNFINISHED: Final[str] = "unfinished"

# 任务成功结束。该状态只能由 root Agent end_task 或用户直接完成当前 Task 的成功意图触发。
AGENT_TASK_STATUS_COMPLETED: Final[str] = "completed"

# 任务失败结束。该状态只能由 fail_task 或用户主动取消任务触发。
AGENT_TASK_STATUS_FAILED: Final[str] = "failed"

AGENT_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_TASK_STATUS_UNFINISHED,
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
})

_TERMINAL_AGENT_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
})

_LEGACY_UNFINISHED_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    "created",
    "running",
    "paused",
})

_LEGACY_FAILED_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    "cancelled",
})


def _normalize_task_status(status: str) -> str:
    """把历史任务执行态归一化为当前任务生命周期三态。"""
    if status in AGENT_TASK_STATUSES:
        return status
    if status in _LEGACY_UNFINISHED_TASK_STATUSES:
        return AGENT_TASK_STATUS_UNFINISHED
    if status in _LEGACY_FAILED_TASK_STATUSES:
        return AGENT_TASK_STATUS_FAILED
    raise ValueError(f"未知 Agent 任务状态: {status}")


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _require_non_blank(value: str, field_name: str) -> None:
    """校验任务运行态标识字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


@dataclass(frozen=True, slots=True)
class AgentParentChildRelation:
    """同一任务内 Agent 实例运行时父子关系。"""

    # 父子关系唯一标识，通常由 task_id、parent_instance_id 和 child_instance_id 组合生成。
    relation_id: str

    # 所属任务 ID。父实例和子实例必须属于同一个任务。
    task_id: str

    # 父 Agent 实例 ID。
    parent_instance_id: str

    # 子 Agent 实例 ID。每个非根实例只能有一个父实例。
    child_instance_id: str

    # 关系创建时间。父子关系是任务执行过程中的事实记录，不通过删除表达生命周期结束。
    created_at: datetime = None  # type: ignore[assignment]

    # 子 Agent 由父 Agent 显式释放或 task 终态清理时写入；为空表示仍处于可通信的活跃租约。
    released_at: datetime | None = None

    # 释放原因摘要，用于区分父 Agent 主动释放、级联释放和 task 终态自动清理。
    release_reason: str | None = None

    # 发起释放的 Agent 实例 ID；task 终态自动清理可为空。
    released_by_instance_id: str | None = None

    def __post_init__(self) -> None:
        """校验父子关系的基础字段。"""
        _require_non_blank(self.relation_id, "relation_id")
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.parent_instance_id, "parent_instance_id")
        _require_non_blank(self.child_instance_id, "child_instance_id")
        if self.parent_instance_id == self.child_instance_id:
            raise ValueError("parent_instance_id 不能等于 child_instance_id。")
        if self.release_reason is not None and not self.release_reason.strip():
            raise ValueError("release_reason 不能是空白字符串。")
        if self.released_by_instance_id is not None:
            _require_non_blank(self.released_by_instance_id, "released_by_instance_id")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())

    @property
    def is_released(self) -> bool:
        """父子关系租约是否已经释放。"""
        return self.released_at is not None


@dataclass(frozen=True, slots=True)
class AgentTask:
    """Agent 任务运行态聚合根。

    一个任务只有一个根 Agent 实例入口，但任务内部可以包含任意数量的 Agent 实例，
    并通过 parent_child_relations 维护一棵运行时 Agent 实例父子关系树。
    任务状态由执行编排层显式维护，不自动根据某个 Agent 实例状态推导。
    """

    # 任务唯一标识。
    task_id: str

    # 任务唯一根 Agent 实例 ID。根实例没有父实例，是任务的入口。
    root_agent_instance_id: str

    # 当前任务中的全部 Agent 实例 ID。包含 root_agent_instance_id，不设数量上限。
    agent_instance_ids: tuple[str, ...]

    # 当前任务中的 Agent 实例父子关系树边集合。根实例不会作为 child 出现。
    parent_child_relations: tuple[AgentParentChildRelation, ...] = ()

    # 当前任务生命周期状态。Task 只表达任务是否结束以及成功/失败结束方式。
    status: AgentTaskStatus = AGENT_TASK_STATUS_UNFINISHED

    # 任务创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次状态或结构更新时间。
    updated_at: datetime = None  # type: ignore[assignment]

    # 任务首次进入执行循环的审计时间；Task 主状态不表达 running，仅保留历史字段。
    started_at: datetime | None = None

    # 任务最近一次由执行循环主动暂停的审计时间；Task 主状态仍保持 unfinished。
    paused_at: datetime | None = None

    # 任务成功完成的时间。用户主动取消会归入 failed_at / failure_message。
    finished_at: datetime | None = None

    # 任务失败结束或用户主动取消的时间。
    failed_at: datetime | None = None

    # 任务失败结束或用户主动取消的原因摘要。仅 failed 状态要求有值。
    failure_message: str | None = None

    # 任务所属场景 ID（scene 模块聚合根）。可选；为 None 表示该任务尚未挂到任何场景。
    # 通信流 ID 由 scene_id 派生（stream-scene-{scene_id}），同一 scene 下多个 task 共享同一通信流，
    # 前端 tab 与 scene 一一对应，刷新页面后通过 scene_id + after_seq 即可恢复完整对话事件流。
    # 兼容性：scene_id 为 None 时执行引擎沿用旧的 stream-agent-task-{task_id} 形式。
    scene_id: str | None = None

    def __post_init__(self) -> None:
        """校验任务内部实例集合与父子关系树的一致性。"""
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.root_agent_instance_id, "root_agent_instance_id")
        normalized_status = _normalize_task_status(self.status)
        if normalized_status != self.status:
            object.__setattr__(self, "status", normalized_status)
        if normalized_status == AGENT_TASK_STATUS_FAILED and not self.failure_message:
            object.__setattr__(self, "failure_message", "任务已失败或被取消。")
        if self.status == AGENT_TASK_STATUS_FAILED:
            _require_non_blank(self.failure_message or "", "failure_message")
        if self.failure_message is not None and not self.failure_message.strip():
            raise ValueError("failure_message 不能是空白字符串。")
        if self.scene_id is not None:
            _require_non_blank(self.scene_id, "scene_id")
        if not self.agent_instance_ids:
            raise ValueError("agent_instance_ids 至少需要包含根 Agent 实例。")
        seen_instances: set[str] = set()
        for instance_id in self.agent_instance_ids:
            _require_non_blank(instance_id, "agent_instance_ids")
            if instance_id in seen_instances:
                raise ValueError(f"agent_instance_ids 存在重复实例: {instance_id}")
            seen_instances.add(instance_id)
        if self.root_agent_instance_id not in seen_instances:
            raise ValueError("root_agent_instance_id 必须包含在 agent_instance_ids 中。")
        self._validate_relation_tree(seen_instances)
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    @property
    def is_terminal(self) -> bool:
        """任务是否已经进入终态。"""
        return self.status in _TERMINAL_AGENT_TASK_STATUSES

    def add_child_relation(
        self,
        *,
        child_instance_id: str,
        parent_instance_id: str,
        relation_id: str,
        at: datetime | None = None,
    ) -> AgentTask:
        """向任务树中追加一个子 Agent 实例及父子关系。"""
        _require_non_blank(child_instance_id, "child_instance_id")
        _require_non_blank(parent_instance_id, "parent_instance_id")
        if child_instance_id in self.agent_instance_ids:
            raise ValueError(f"Agent 实例已属于任务: {child_instance_id}")
        if parent_instance_id not in self.agent_instance_ids:
            raise ValueError(f"父 Agent 实例不属于任务: {parent_instance_id}")
        relation = AgentParentChildRelation(
            relation_id=relation_id,
            task_id=self.task_id,
            parent_instance_id=parent_instance_id,
            child_instance_id=child_instance_id,
            created_at=at or _utc_now(),
        )
        return replace(
            self,
            agent_instance_ids=(*self.agent_instance_ids, child_instance_id),
            parent_child_relations=(*self.parent_child_relations, relation),
            updated_at=at or _utc_now(),
        )

    def mark_started(
        self,
        *,
        at: datetime | None = None,
    ) -> "AgentTask":
        """幂等记录 Task 首次真正进入 Agent 执行循环的审计时间。"""
        if self.started_at is not None:
            return self
        if self.is_terminal:
            raise ValueError(f"已结束的 Agent Task 不能重新标记为开始: {self.task_id}")
        now = at or _utc_now()
        return replace(self, started_at=now, updated_at=now)

    def transition_to(
        self,
        target_status: AgentTaskStatus,
        *,
        failure_message: str | None = None,
        at: datetime | None = None,
    ) -> AgentTask:
        """按任务生命周期三态流转。

        Task 不再承载 running/paused 这类执行态；这些由 AgentInstance 与执行循环负责。
        因此 start_task/pause_task/resume_task 对 Task 来说都是保持 unfinished 的幂等操作。
        """
        target_status = _normalize_task_status(target_status)
        if target_status == self.status:
            return self
        if self.is_terminal:
            raise ValueError(f"Agent 任务状态不能从 {self.status} 流转到 {target_status}。")
        if self.status != AGENT_TASK_STATUS_UNFINISHED:
            raise ValueError(f"未知 Agent 任务状态: {self.status}")
        if target_status not in _TERMINAL_AGENT_TASK_STATUSES:
            raise ValueError(f"Agent 任务状态不能从 {self.status} 流转到 {target_status}。")
        now = at or _utc_now()
        values: dict[str, object] = {
            "status": target_status,
            "updated_at": now,
        }
        if target_status == AGENT_TASK_STATUS_COMPLETED:
            values["finished_at"] = now
            values["failure_message"] = None
        elif target_status == AGENT_TASK_STATUS_FAILED:
            _require_non_blank(failure_message or "", "failure_message")
            values["failed_at"] = now
            values["failure_message"] = failure_message
        return replace(self, **values)

    def parent_of(self, instance_id: str, *, include_released: bool = True) -> str | None:
        """查询指定实例的父实例 ID；根实例返回 None。

        include_released=False 时只把尚未释放的父子关系视为有效父关系，供子 Agent 通信、
        展示和释放权限判断使用；默认保留历史行为，按任务关系事实返回父实例。
        """
        if instance_id == self.root_agent_instance_id:
            return None
        for relation in self.parent_child_relations:
            if relation.child_instance_id == instance_id and (include_released or not relation.is_released):
                return relation.parent_instance_id
        return None

    def children_of(self, instance_id: str, *, include_released: bool = True) -> tuple[str, ...]:
        """查询指定实例的直接子实例 ID 列表。

        默认返回完整历史关系；include_released=False 时只返回仍未释放的直接子 Agent。
        """
        return tuple(
            relation.child_instance_id
            for relation in self.parent_child_relations
            if relation.parent_instance_id == instance_id and (include_released or not relation.is_released)
        )

    def unreleased_children_of(self, instance_id: str) -> tuple[str, ...]:
        """查询指定实例当前仍未释放的直接子 Agent 实例 ID。"""
        return self.children_of(instance_id, include_released=False)

    def child_relation_of(self, child_instance_id: str) -> AgentParentChildRelation | None:
        """查询子实例对应的父子关系；根实例或未知实例返回 None。"""
        for relation in self.parent_child_relations:
            if relation.child_instance_id == child_instance_id:
                return relation
        return None

    def release_child_tree(
        self,
        child_instance_id: str,
        *,
        released_by_instance_id: str | None,
        reason: str,
        at: datetime | None = None,
    ) -> "AgentTask":
        """释放某个子 Agent 及其尚未释放的后代租约。

        父子关系作为历史事实保留，释放只写入 relation 的 release 字段。释放子树可以避免
        孙级 Agent 在父 Agent 被释放后继续作为活跃实例悬挂。
        """
        _require_non_blank(child_instance_id, "child_instance_id")
        _require_non_blank(reason, "release_reason")
        if released_by_instance_id is not None:
            _require_non_blank(released_by_instance_id, "released_by_instance_id")
        target_relation = self.child_relation_of(child_instance_id)
        if target_relation is None:
            raise ValueError(f"子 Agent 实例不属于任务父子关系树: {child_instance_id}")
        if target_relation.is_released:
            return self
        release_at = at or _utc_now()
        release_child_ids = self._collect_unreleased_descendants(child_instance_id)
        release_child_ids.add(child_instance_id)
        updated_relations = tuple(
            replace(
                relation,
                released_at=release_at,
                release_reason=reason,
                released_by_instance_id=released_by_instance_id,
            )
            if relation.child_instance_id in release_child_ids and not relation.is_released
            else relation
            for relation in self.parent_child_relations
        )
        return replace(self, parent_child_relations=updated_relations, updated_at=release_at)

    def release_all_children(
        self,
        *,
        released_by_instance_id: str | None,
        reason: str,
        at: datetime | None = None,
    ) -> "AgentTask":
        """释放任务内全部尚未释放的子 Agent 租约。"""
        _require_non_blank(reason, "release_reason")
        if released_by_instance_id is not None:
            _require_non_blank(released_by_instance_id, "released_by_instance_id")
        if not any(not relation.is_released for relation in self.parent_child_relations):
            return self
        release_at = at or _utc_now()
        updated_relations = tuple(
            replace(
                relation,
                released_at=release_at,
                release_reason=reason,
                released_by_instance_id=released_by_instance_id,
            )
            if not relation.is_released
            else relation
            for relation in self.parent_child_relations
        )
        return replace(self, parent_child_relations=updated_relations, updated_at=release_at)

    def _collect_unreleased_descendants(self, instance_id: str) -> set[str]:
        """收集某实例下尚未释放的所有后代子实例 ID。"""
        descendants: set[str] = set()
        stack = list(self.unreleased_children_of(instance_id))
        while stack:
            child_id = stack.pop()
            if child_id in descendants:
                continue
            descendants.add(child_id)
            stack.extend(self.unreleased_children_of(child_id))
        return descendants

    def _validate_relation_tree(self, instance_ids: set[str]) -> None:
        """校验父子关系集合满足单根树约束。"""
        relation_ids: set[str] = set()
        child_ids: set[str] = set()
        edges: dict[str, list[str]] = {instance_id: [] for instance_id in instance_ids}
        for relation in self.parent_child_relations:
            if relation.task_id != self.task_id:
                raise ValueError("父子关系 task_id 必须与任务 task_id 一致。")
            if relation.relation_id in relation_ids:
                raise ValueError(f"父子关系 ID 重复: {relation.relation_id}")
            relation_ids.add(relation.relation_id)
            if relation.parent_instance_id not in instance_ids:
                raise ValueError(f"父实例不属于任务: {relation.parent_instance_id}")
            if relation.child_instance_id not in instance_ids:
                raise ValueError(f"子实例不属于任务: {relation.child_instance_id}")
            if relation.child_instance_id == self.root_agent_instance_id:
                raise ValueError("根 Agent 实例不能作为子实例。")
            if relation.child_instance_id in child_ids:
                raise ValueError(f"子实例只能有一个父实例: {relation.child_instance_id}")
            child_ids.add(relation.child_instance_id)
            edges[relation.parent_instance_id].append(relation.child_instance_id)
        expected_child_ids = instance_ids - {self.root_agent_instance_id}
        if child_ids != expected_child_ids:
            missing = sorted(expected_child_ids - child_ids)
            if missing:
                raise ValueError(f"非根 Agent 实例必须有父实例: {', '.join(missing)}")
        self._ensure_no_cycle(edges)

    def _ensure_no_cycle(self, edges: dict[str, list[str]]) -> None:
        """从根实例遍历整棵树，确保无环且所有实例可达。"""
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(instance_id: str) -> None:
            if instance_id in visiting:
                raise ValueError(f"Agent 父子关系不能形成环: {instance_id}")
            if instance_id in visited:
                return
            visiting.add(instance_id)
            for child_id in edges.get(instance_id, []):
                visit(child_id)
            visiting.remove(instance_id)
            visited.add(instance_id)

        visit(self.root_agent_instance_id)
        missing = set(self.agent_instance_ids) - visited
        if missing:
            raise ValueError(f"Agent 父子关系树存在不可达实例: {', '.join(sorted(missing))}")
