# -*- coding: utf-8 -*-
"""Task Note 观察者执行会话目标 Agent 绑定上下文 (M5)。"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


@dataclass(slots=True)
class ObserverBinding:
    """观察会话上下文绑定，记录目标业务 Agent 身份与本次观察的切片 units。"""

    task_id: str
    target_agent_instance_id: str
    scene_id: str = ""
    units: list[Any] = field(default_factory=list)
    observer_min_advance_ratio: float = 0.5


_current_observer_binding: contextvars.ContextVar[ObserverBinding | None] = contextvars.ContextVar(
    "_current_observer_binding", default=None
)


def get_current_observer_binding() -> ObserverBinding | None:
    """获取当前协程/执行上下文中的观察者绑定。"""
    return _current_observer_binding.get()


def set_current_observer_binding(binding: ObserverBinding | None) -> contextvars.Token[ObserverBinding | None]:
    """设置当前协程/执行上下文中的观察者绑定。"""
    return _current_observer_binding.set(binding)


def reset_current_observer_binding(token: contextvars.Token[ObserverBinding | None]) -> None:
    """恢复观察者绑定上下文。"""
    _current_observer_binding.reset(token)


def resolve_note_tool_target(ctx: ToolRuntimeContext | None) -> tuple[str, str, str]:
    """解析当前 Note 工具调用的目标上下文 (task_id, agent_instance_id, scene_id)。

    优先从 ObserverBinding 读取 (M5 观察者链路)。
    若无全局 binding，则从 ctx 校验并提取目标 ID；杜绝任意默认静态 ID 兜底。
    若无有效绑定与目标，则抛出 ValueError 显式拒绝调用。
    """
    binding = get_current_observer_binding()
    if binding is not None:
        if not binding.task_id or not binding.target_agent_instance_id:
            raise ValueError("观察者绑定缺少有效 task_id 或 target_agent_instance_id，拒绝执行 Note 工具")
        scene_id = binding.scene_id or f"scene-{binding.task_id}"
        return binding.task_id, binding.target_agent_instance_id, scene_id

    if ctx is not None:
        task_id = getattr(ctx, "task_id", None)
        agent_instance_id = getattr(ctx, "agent_instance_id", None)
        if task_id and agent_instance_id:
            scene_id = getattr(ctx, "scene_id", None) or f"scene-{task_id}"
            return task_id, agent_instance_id, scene_id

    raise ValueError("Note 维护工具未绑定目标业务 Agent，禁止直接调用 (M5 权限校验失败)")
