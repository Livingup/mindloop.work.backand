# -*- coding: utf-8 -*-
"""Task Note 观察调度器 (TaskNoteObserverScheduler)。

核心职责：
1. 监控业务 Agent 的消息增量，基于 TaskNoteUnitSlicer 判断是否达到 80k Token 门槛；
2. 并发保护：确保同一业务 Agent 实例在同一时刻至多只有一个 Observer 处于运行态；
3. 后台并行启动 Observer 会话：注入最新 Note 快照、增量消息 Units 及子 Agent 继承段与委派原文；
4. 校验 Observer 返回的 covered_until_seq 并安全推进 task_note_watermark，推进失败不阻断业务；
5. 提供与业务 Agent 两次 LLM 调用之间的热替换协调接口。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from agent_runtime.task_note.task_note_models import (
    SliceResult,
    TaskNoteUnit,
    TaskNoteWatermark,
)
from agent_runtime.task_note.task_note_prompt_provider import (
    TaskNotePromptProvider,
)
from agent_runtime.task_note.task_note_store import (
    TaskNoteStore,
    get_task_note_store,
)
from agent_runtime.task_note.task_note_unit_slicer import (
    DEFAULT_OBSERVER_TRIGGER_TOKENS,
    TaskNoteUnitSlicer,
)

logger = logging.getLogger(__name__)


@dataclass
class ObserverSessionInput:
    """观察者会话启动入参上下文。"""

    task_id: str
    agent_instance_id: str
    target_seq: int
    note_snapshot: dict[str, Any]
    incremental_units: list[TaskNoteUnit] = field(default_factory=list)
    inherited_units: list[TaskNoteUnit] = field(default_factory=list)
    delegation_content: str | None = None
    fork_message_id: str | None = None
    is_subagent: bool = False


@dataclass
class ObserverSessionResult:
    """观察者会话执行输出。"""

    success: bool
    covered_until_seq: int = 0
    note_version: int = 0
    error_message: str | None = None
    tools_executed_count: int = 0


def format_units_for_observer(units: Sequence[TaskNoteUnit]) -> str:
    """将消息切片单元格式化为带 seq 编号与 auto_record 提示的高可读性观察日志文本。"""
    if not units:
        return "（无新消息）"

    blocks: list[str] = []
    for u in units:
        header = f"--- [Unit seq={u.seq} | type={u.unit_type} | end_msg_id={u.end_message_id} | tokens≈{u.token_count}] ---"
        msg_lines: list[str] = []
        for m in u.messages:
            role = ""
            content = ""
            auto_records: list[str] = []

            if isinstance(m, dict):
                role = str(m.get("role") or "").upper()
                raw_c = m.get("content")
                content = str(raw_c or "")
                # 检查 output / content 中的 auto_record
                ar = m.get("auto_record")
                if ar:
                    auto_records.append(json.dumps(ar, ensure_ascii=False))
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    content += f"\n  [Tool Calls]: {json.dumps(tool_calls, ensure_ascii=False)}"
            else:
                role = str(getattr(m, "role", "") or "").upper()
                content = str(getattr(m, "content", "") or "")
                ar = getattr(m, "auto_record", None)
                if ar:
                    auto_records.append(json.dumps(ar, ensure_ascii=False, default=str))
                tool_calls = getattr(m, "tool_calls", None)
                if tool_calls:
                    content += f"\n  [Tool Calls]: {json.dumps(tool_calls, ensure_ascii=False, default=str)}"

            line = f"【{role}】: {content}"
            if auto_records:
                line += f"\n  ⚡ [程序底层已自动插入 auto_record]: {', '.join(auto_records)} (请绑定 mission_id 或按主键清理)"
            msg_lines.append(line)

        block_text = f"{header}\n" + "\n".join(msg_lines)
        blocks.append(block_text)

    return "\n\n".join(blocks)


def build_observer_user_payload(
    session_input: ObserverSessionInput,
    target_agent_definition_prompt: str = "",
) -> str:
    """构造观察者会话启动时的首条 User 消息完整文本。"""
    clean_snapshot = {k: v for k, v in session_input.note_snapshot.items() if k != "commands"}
    snapshot_json_str = json.dumps(clean_snapshot, ensure_ascii=False, indent=2)

    parts: list[str] = [
        "# 观察任务输入上下文\n",
        "## 1. 目标 Agent 身份与职责定义",
        f"- 实例 ID: `{session_input.agent_instance_id}`",
        f"- 所属任务 ID: `{session_input.task_id}`",
        f"- 角色模式: `{'子 Agent (Subagent)' if session_input.is_subagent else '根业务 Agent'}`",
    ]

    if session_input.delegation_content:
        parts.append(f"- 直接父级委派原文: `{session_input.delegation_content}`")

    if target_agent_definition_prompt:
        parts.append(
            "- 目标 Agent 业务职责定义 (用于理解其专业领域与产物，切勿代入该角色):\n"
            f"> {target_agent_definition_prompt}\n"
        )

    parts.append(
        "## 2. 当前 Task Note 基准线快照 (Baseline)\n"
        "以下是数据库当前已落库的 Note 结构化快照。你必须以它为基准线进行增量或修订：\n"
        f"```json\n{snapshot_json_str}\n```\n"
    )

    # 3. 对话消息流（自然呈现）
    if session_input.is_subagent and session_input.inherited_units:
        inherited_text = format_units_for_observer(session_input.inherited_units)
        inc_text = format_units_for_observer(session_input.incremental_units)
        parts.append(
            "## 3. 会话消息流水（含分叉背景与增量执行）\n"
            "### (1) 上层继承背景消息 (Inherited Units - 仅作为事实与硬约束参考，勿当作当前 Agent 的产物):\n"
            f"{inherited_text}\n\n"
            "### (2) 目标 Agent 增量对话切片 (Incremental Units - 本次重点观察与提炼范围):\n"
            f"{inc_text}\n"
        )
    else:
        inc_text = format_units_for_observer(session_input.incremental_units)
        parts.append(
            "## 3. 待观察的增量对话切片 (Incremental Units)\n"
            f"以下是上一断点以来的新增对话。每个 Unit 是一个原子切片，其末尾标有合法断点 seq (本次最高目标断点为 seq={session_input.target_seq})：\n"
            f"{inc_text}\n"
        )

    parts.append(
        "---\n"
        "请通读上述内容，调用专属 Note 工具维护 11 张 Note 业务表。完成后必须调用 `note_update_end(covered_until_seq=...)` 提交断点并安全退出。"
    )

    return "\n\n".join(parts)


class TaskNoteObserverScheduler:
    """Task Note 观察调度器。"""

    def __init__(
        self,
        *,
        slicer: TaskNoteUnitSlicer | None = None,
        store: TaskNoteStore | None = None,
        prompt_provider: TaskNotePromptProvider | None = None,
        observer_runner: Callable[[ObserverSessionInput], Awaitable[ObserverSessionResult]] | None = None,
        trigger_threshold_tokens: int = DEFAULT_OBSERVER_TRIGGER_TOKENS,
    ) -> None:
        self.slicer = slicer or TaskNoteUnitSlicer(trigger_threshold_tokens=trigger_threshold_tokens)
        self.store = store or get_task_note_store()
        self.prompt_provider = prompt_provider or TaskNotePromptProvider()
        self._observer_runner = observer_runner
        self._running_tasks: dict[str, asyncio.Task[ObserverSessionResult]] = {}
        self._latest_results: dict[str, ObserverSessionResult] = {}
        self._lock = asyncio.Lock()

    def is_observer_running(self, agent_instance_id: str) -> bool:
        """检查目标业务 Agent 是否已有观察者正在后台运行。"""
        task = self._running_tasks.get(agent_instance_id)
        if task is None:
            return False
        return not task.done()

    def get_running_observer_count(self) -> int:
        """获取当前后台正在运行的观察者数量。"""
        return sum(1 for t in self._running_tasks.values() if not t.done())

    async def check_and_trigger(
        self,
        *,
        agent_instance_id: str,
        task_id: str,
        messages: Sequence[Any],
        is_subagent: bool = False,
        fork_message_id: str | None = None,
        delegation_content: str | None = None,
    ) -> bool:
        """检查增量 Token 是否满足触发条件；若满足且当前无观察者运行，则异步触发后台观察。
        
        返回 True 表示已成功触发后台观察；False 表示未达阈值或已有观察者运行中。
        """
        async with self._lock:
            # 1. 并发保护：若已有运行中的观察者，跳过
            if self.is_observer_running(agent_instance_id):
                logger.debug("业务 Agent %s 观察者已在运行中，跳过重复触发", agent_instance_id)
                return False

            # 2. 读取当前 watermark
            watermark = self.store.get_watermark(agent_instance_id=agent_instance_id, task_id=task_id)

            # 3. 切片并计算增量
            slice_res = self.slicer.slice_messages(
                messages=messages,
                watermark_covered_until_seq=watermark.covered_until_seq,
                covered_until_message_id=watermark.covered_until_message_id,
                is_subagent=is_subagent,
                inherited_processed=watermark.inherited_processed,
                fork_message_id=fork_message_id,
            )

            # 4. 判断触发条件
            if not slice_res.should_trigger:
                logger.debug(
                    "业务 Agent %s 增量 Token (%d) 未达阈值 (%d)，无需触发观察",
                    agent_instance_id,
                    slice_res.incremental_tokens,
                    self.slicer.trigger_threshold_tokens,
                )
                return False

            # 5. 组装输入
            snapshot = self.store.get_note_snapshot(task_id=task_id, agent_instance_id=agent_instance_id)
            session_input = ObserverSessionInput(
                task_id=task_id,
                agent_instance_id=agent_instance_id,
                target_seq=slice_res.max_seq,
                note_snapshot=snapshot,
                incremental_units=slice_res.incremental_units,
                inherited_units=slice_res.inherited_units,
                delegation_content=delegation_content,
                fork_message_id=fork_message_id,
                is_subagent=is_subagent,
            )

            # 6. 后台并行启动观察任务
            task = asyncio.create_task(self._run_observer_session(session_input))
            self._running_tasks[agent_instance_id] = task
            logger.info(
                "已为业务 Agent %s 触发后台观察: 增量 Tokens=%d, 目标 seq=%d",
                agent_instance_id,
                slice_res.incremental_tokens,
                slice_res.max_seq,
            )
            return True

    async def _run_observer_session(self, session_input: ObserverSessionInput) -> ObserverSessionResult:
        """执行单个观察者会话全生命周期并收尾。"""
        agent_id = session_input.agent_instance_id
        task_id = session_input.task_id
        try:
            if self._observer_runner is not None:
                result = await self._observer_runner(session_input)
            else:
                # 默认模拟/基础执行：推进到 target_seq
                result = ObserverSessionResult(
                    success=True,
                    covered_until_seq=session_input.target_seq,
                    note_version=1,
                    tools_executed_count=0,
                )

            # 校验阶段：covered_until_seq 合法性检查
            current_watermark = self.store.get_watermark(agent_instance_id=agent_id, task_id=task_id)
            if not result.success:
                logger.warning("观察者会话执行失败: %s", result.error_message)
                self._latest_results[agent_id] = result
                return result

            if result.covered_until_seq <= current_watermark.covered_until_seq:
                logger.warning(
                    "观察者提交的 seq=%d 未能超过当前水位 %d，水位不推进",
                    result.covered_until_seq,
                    current_watermark.covered_until_seq,
                )
                result.success = False
                result.error_message = f"covered_until_seq ({result.covered_until_seq}) <= 当前水位 ({current_watermark.covered_until_seq})"
                self._latest_results[agent_id] = result
                return result

            if result.covered_until_seq > session_input.target_seq:
                logger.warning(
                    "观察者提交的 seq=%d 超过了本次最大可覆盖目标 seq=%d，强制校正为 %d",
                    result.covered_until_seq,
                    session_input.target_seq,
                    session_input.target_seq,
                )
                result.covered_until_seq = session_input.target_seq

            # 校验通过：原子推进 Watermark
            covered_msg_id = None
            all_input_units = session_input.inherited_units + session_input.incremental_units
            for u in all_input_units:
                if u.seq == result.covered_until_seq:
                    covered_msg_id = u.end_message_id
                    break

            wm_info = self.store.update_watermark(
                agent_instance_id=agent_id,
                task_id=task_id,
                covered_until_seq=result.covered_until_seq,
                covered_until_message_id=covered_msg_id,
                note_version_inc=True,
            )
            result.note_version = wm_info.get("note_version", current_watermark.note_version + 1)

            # 子 Agent 首次观察完成后标记 inherited_processed
            if session_input.is_subagent and not current_watermark.inherited_processed:
                self.store.set_inherited_processed(agent_instance_id=agent_id, task_id=task_id)

            logger.info(
                "观察者会话完成并校验通过: agent=%s, 新水位 seq=%d, version=%d",
                agent_id,
                result.covered_until_seq,
                result.note_version,
            )
            self._latest_results[agent_id] = result
            return result

        except Exception as exc:
            logger.error("观察者会话执行发生未捕获异常: %s", exc, exc_info=True)
            res = ObserverSessionResult(success=False, error_message=str(exc))
            self._latest_results[agent_id] = res
            return res
        finally:
            async with self._lock:
                self._running_tasks.pop(agent_id, None)

    async def wait_for_observer(
        self, agent_instance_id: str, timeout: float | None = None
    ) -> ObserverSessionResult | None:
        """等待目标 Agent 的观察任务执行完成（用于测试或安全点同步）。"""
        task = self._running_tasks.get(agent_instance_id)
        if task is None:
            return self._latest_results.get(agent_instance_id)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("等待 Agent %s 的观察者超时 (timeout=%s)", agent_instance_id, timeout)
            return None

    def get_latest_result(self, agent_instance_id: str) -> ObserverSessionResult | None:
        """获取最近一次观察会话的结果。"""
        return self._latest_results.get(agent_instance_id)
