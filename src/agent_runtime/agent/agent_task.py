# -*- coding: utf-8 -*-
"""Agent 任务与父子实例关系领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final, TypeAlias


AgentTaskStatus: TypeAlias = str

# 任务已创建且已有根 Agent 实例，但尚未进入执行。
AGENT_TASK_STATUS_CREATED: Final[str] = "created"

# 任务正在执行。任务内允许多个 Agent 实例并发 running。
AGENT_TASK_STATUS_RUNNING: Final[str] = "running"

# 任务整体暂停。任务内实例状态仍由执行编排层分别维护。
AGENT_TASK_STATUS_PAUSED: Final[str] = "paused"

# 任务正常完成。该状态必须由执行编排层显式标记。
AGENT_TASK_STATUS_COMPLETED: Final[str] = "completed"

# 任务失败，failure_message 保存失败原因摘要。
AGENT_TASK_STATUS_FAILED: Final[str] = "failed"

# 任务被取消。
AGENT_TASK_STATUS_CANCELLED: Final[str] = "cancelled"

AGENT_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_TASK_STATUS_CREATED,
    AGENT_TASK_STATUS_RUNNING,
    AGENT_TASK_STATUS_PAUSED,
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_CANCELLED,
})

_TERMINAL_AGENT_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_CANCELLED,
})

_AGENT_TASK_STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    AGENT_TASK_STATUS_CREATED: frozenset({
        AGENT_TASK_STATUS_RUNNING,
        AGENT_TASK_STATUS_FAILED,
        AGENT_TASK_STATUS_CANCELLED,
    }),
    AGENT_TASK_STATUS_RUNNING: frozenset({
        AGENT_TASK_STATUS_PAUSED,
        AGENT_TASK_STATUS_COMPLETED,
        AGENT_TASK_STATUS_FAILED,
        AGENT_TASK_STATUS_CANCELLED,
    }),
    AGENT_TASK_STATUS_PAUSED: frozenset({
        AGENT_TASK_STATUS_RUNNING,
        AGENT_TASK_STATUS_FAILED,
        AGENT_TASK_STATUS_CANCELLED,
    }),
    AGENT_TASK_STATUS_COMPLETED: frozenset(),
    AGENT_TASK_STATUS_FAILED: frozenset(),
    AGENT_TASK_STATUS_CANCELLED: frozenset(),
}


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

    # 关系创建时间。父子关系是任务执行过程中的事实记录，本轮不提供删除能力。
    created_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """校验父子关系的基础字段。"""
        _require_non_blank(self.relation_id, "relation_id")
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.parent_instance_id, "parent_instance_id")
        _require_non_blank(self.child_instance_id, "child_instance_id")
        if self.parent_instance_id == self.child_instance_id:
            raise ValueError("parent_instance_id 不能等于 child_instance_id。")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())


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

    # 当前任务状态。任务状态与实例状态解耦，由执行编排层显式触发。
    status: AgentTaskStatus = AGENT_TASK_STATUS_CREATED

    # 任务创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次状态或结构更新时间。
    updated_at: datetime = None  # type: ignore[assignment]

    # 首次进入 running 的时间。
    started_at: datetime | None = None

    # 最近一次进入 paused 的时间。
    paused_at: datetime | None = None

    # 正常完成或取消的时间。
    finished_at: datetime | None = None

    # 进入 failed 的时间。
    failed_at: datetime | None = None

    # 失败原因摘要。仅 failed 状态要求有值。
    failure_message: str | None = None

    def __post_init__(self) -> None:
        """校验任务内部实例集合与父子关系树的一致性。"""
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.root_agent_instance_id, "root_agent_instance_id")
        if self.status not in AGENT_TASK_STATUSES:
            raise ValueError(f"未知 Agent 任务状态: {self.status}")
        if self.status == AGENT_TASK_STATUS_FAILED:
            _require_non_blank(self.failure_message or "", "failure_message")
        if self.failure_message is not None and not self.failure_message.strip():
            raise ValueError("failure_message 不能是空白字符串。")
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

    def transition_to(
        self,
        target_status: AgentTaskStatus,
        *,
        failure_message: str | None = None,
        at: datetime | None = None,
    ) -> AgentTask:
        """按领域允许的任务状态机流转到目标状态。"""
        if target_status not in AGENT_TASK_STATUSES:
            raise ValueError(f"未知 Agent 任务状态: {target_status}")
        if target_status == self.status:
            return self
        allowed_targets = _AGENT_TASK_STATUS_TRANSITIONS[self.status]
        if target_status not in allowed_targets:
            raise ValueError(f"Agent 任务状态不能从 {self.status} 流转到 {target_status}。")
        now = at or _utc_now()
        values: dict[str, object] = {
            "status": target_status,
            "updated_at": now,
        }
        if target_status == AGENT_TASK_STATUS_RUNNING:
            values["started_at"] = self.started_at or now
        elif target_status == AGENT_TASK_STATUS_PAUSED:
            values["paused_at"] = now
        elif target_status == AGENT_TASK_STATUS_COMPLETED:
            values["finished_at"] = now
            values["failure_message"] = None
        elif target_status == AGENT_TASK_STATUS_FAILED:
            _require_non_blank(failure_message or "", "failure_message")
            values["failed_at"] = now
            values["failure_message"] = failure_message
        elif target_status == AGENT_TASK_STATUS_CANCELLED:
            values["finished_at"] = now
        return replace(self, **values)

    def parent_of(self, instance_id: str) -> str | None:
        """查询指定实例的父实例 ID；根实例返回 None。"""
        if instance_id == self.root_agent_instance_id:
            return None
        for relation in self.parent_child_relations:
            if relation.child_instance_id == instance_id:
                return relation.parent_instance_id
        return None

    def children_of(self, instance_id: str) -> tuple[str, ...]:
        """查询指定实例的直接子实例 ID 列表。"""
        return tuple(
            relation.child_instance_id
            for relation in self.parent_child_relations
            if relation.parent_instance_id == instance_id
        )

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
