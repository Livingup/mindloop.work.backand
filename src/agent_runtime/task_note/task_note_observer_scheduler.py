# -*- coding: utf-8 -*-
"""Task Note 观察调度器 (TaskNoteObserverScheduler)。

核心职责：
1. 监控业务 Agent 的消息增量，基于 TaskNoteUnitSlicer 判断是否达到 80k Token 门槛；
2. 并发保护：确保同一业务 Agent 实例在同一时刻至多只有一个 Observer 处于运行态；
3. 后台并行启动 Observer 会话：注入最新 Note 快照、增量消息 Units 及子 Agent 继承段与委派原文；
4. 调度真实 ObserverRunner 执行，水位仅由 note_update_end 校验通过后推进；
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
from agent_runtime.task_note.task_note_tracking_coordinator import (
    ObserveTicket,
    get_task_note_tracking_coordinator,
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
    run_id: str = ""
    conversation_id: str = ""
    incremental_units: list[TaskNoteUnit] = field(default_factory=list)
    inherited_units: list[TaskNoteUnit] = field(default_factory=list)
    delegation_content: str | None = None
    fork_message_id: str | None = None
    is_subagent: bool = False
    parent_agent_instance_id: str | None = None
    ancestor_chain: list[str] = field(default_factory=list)
    target_agent_definition_prompt: str | None = None
    scene_id: str | None = None


@dataclass
class ObserverSessionResult:
    """观察者会话执行输出。"""

    success: bool
    covered_until_seq: int = 0
    note_version: int = 0
    error_message: str | None = None
    tools_executed_count: int = 0


def _extract_msg_text(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, dict):
        c = msg.get("text") if msg.get("text") is not None else msg.get("content")
        return str(c or "")
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if hasattr(part, "text") and part.text:
                text_parts.append(str(part.text))
            elif isinstance(part, dict) and part.get("text"):
                text_parts.append(str(part["text"]))
        return "\n".join(text_parts)
    return str(getattr(msg, "text", "") or "")


def serialize_units_to_list(units: Sequence[TaskNoteUnit]) -> list[dict[str, Any]]:
    """将 TaskNoteUnit 序列转换为符合文档 1 §9.3 规范的字典列表。"""
    result: list[dict[str, Any]] = []
    for u in units:
        unit_dict: dict[str, Any] = {"seq": u.seq, "messages": []}
        for m in u.messages:
            msg_dict: dict[str, Any] = {}
            if isinstance(m, dict):
                msg_dict["role"] = m.get("role") or "user"
                if m.get("source"):
                    msg_dict["source"] = m.get("source")
                if m.get("source_id"):
                    msg_dict["source_id"] = m.get("source_id")
                content = m.get("text") if m.get("text") is not None else m.get("content")
                if content is not None:
                    msg_dict["text"] = str(content)
                if m.get("tool_calls"):
                    msg_dict["tool_calls"] = m.get("tool_calls")
                if m.get("call_id"):
                    msg_dict["call_id"] = m.get("call_id")
                if m.get("status"):
                    msg_dict["status"] = m.get("status")
                if m.get("result"):
                    msg_dict["result"] = str(m.get("result"))
                if m.get("truncated"):
                    msg_dict["truncated"] = m.get("truncated")
                if m.get("auto_record"):
                    msg_dict["auto_record"] = m.get("auto_record")
            else:
                msg_dict["role"] = getattr(m, "role", "user")
                if hasattr(m, "source") and m.source:
                    msg_dict["source"] = m.source
                if hasattr(m, "source_id") and m.source_id:
                    msg_dict["source_id"] = m.source_id

                content = getattr(m, "content", None)
                if isinstance(content, list):
                    text_parts: list[str] = []
                    for part in content:
                        part_type = type(part).__name__
                        if part_type == "ToolUsePart" or hasattr(part, "tool_use_id"):
                            tid = getattr(part, "tool_use_id", None) or getattr(part, "id", "")
                            tname = getattr(part, "name", "")
                            targs = (
                                getattr(part, "input", None)
                                if getattr(part, "input", None) is not None
                                else (
                                    getattr(part, "arguments", None)
                                    if getattr(part, "arguments", None) is not None
                                    else getattr(part, "args", {}) or {}
                                )
                            )
                            msg_dict["tool_use"] = {"id": tid, "name": tname, "input": targs}
                        elif part_type == "ToolResultPart" or hasattr(part, "tool_use_ref"):
                            ref = getattr(part, "tool_use_ref", None) or getattr(part, "ref", "")
                            is_err = getattr(part, "is_error", False)
                            out = getattr(part, "output", None)
                            msg_dict["tool_result"] = {"ref": ref, "is_error": is_err, "output": out}
                            if isinstance(out, dict) and out.get("auto_record"):
                                msg_dict["auto_record"] = out["auto_record"]
                        elif hasattr(part, "text"):
                            t = getattr(part, "text", "")
                            if t:
                                text_parts.append(str(t))
                        else:
                            text_parts.append(str(part))
                    if text_parts:
                        msg_dict["text"] = "\n".join(text_parts)
                else:
                    text = _extract_msg_text(m)
                    if text:
                        msg_dict["text"] = text

                if hasattr(m, "tool_calls") and m.tool_calls:
                    msg_dict["tool_calls"] = m.tool_calls
                if hasattr(m, "call_id") and m.call_id:
                    msg_dict["call_id"] = m.call_id
                if hasattr(m, "status") and m.status:
                    msg_dict["status"] = m.status
                if hasattr(m, "result") and m.result:
                    msg_dict["result"] = str(m.result)
                if hasattr(m, "truncated") and m.truncated:
                    msg_dict["truncated"] = m.truncated
                if hasattr(m, "auto_record") and m.auto_record and "auto_record" not in msg_dict:
                    msg_dict["auto_record"] = m.auto_record
            unit_dict["messages"].append(msg_dict)
        result.append(unit_dict)
    return result


def format_units_for_observer(units: Sequence[TaskNoteUnit]) -> str:
    """按文档 1 §9.3 序列化 units 结构化文本。"""
    if not units:
        return '{\n  "units": []\n}'
    data = {"units": serialize_units_to_list(units)}
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_observer_user_payload(
    session_input: ObserverSessionInput,
    target_agent_definition_prompt: str = "",
) -> str:
    """构造观察者会话启动时的首条 User 消息完整文本 (包含文档 1 §9.2 规定的 5 类内容)。"""
    clean_snapshot = {k: v for k, v in session_input.note_snapshot.items() if k != "commands"}
    snapshot_json_str = json.dumps(clean_snapshot, ensure_ascii=False, indent=2)

    # 1. 目标 Agent 身份
    parts: list[str] = [
        "# 观察任务输入上下文\n",
        "## 1. 目标 Agent 身份与职责定义",
        f"- 实例 ID: `{session_input.agent_instance_id}`",
        f"- 所属任务 ID: `{session_input.task_id}`",
        f"- 角色模式: `{'子 Agent (Subagent)' if session_input.is_subagent else '根业务 Agent'}`",
    ]
    if session_input.parent_agent_instance_id:
        parts.append(f"- 父 Agent ID: `{session_input.parent_agent_instance_id}`")
    if session_input.ancestor_chain:
        parts.append(f"- 祖先链: {json.dumps(session_input.ancestor_chain, ensure_ascii=False)}")
    if session_input.delegation_content:
        parts.append(f"- 直接父级委派原文: `{session_input.delegation_content}`")

    # 2. 目标 Agent 业务职责定义 (目标定义提示词)
    def_prompt = target_agent_definition_prompt or session_input.target_agent_definition_prompt or ""
    if def_prompt:
        parts.append(
            "- 目标 Agent 业务职责定义 (用于理解其专业领域与产物，切勿代入该角色):\n"
            f"> {def_prompt}\n"
        )

    # 3. 当前 Task Note 基准线快照 (Baseline)
    parts.append(
        "## 2. 当前 Task Note 基准线快照 (Baseline)\n"
        "以下是数据库当前已落库的 Note 结构化快照。你必须以它为基准线进行增量或修订：\n"
        f"```json\n{snapshot_json_str}\n```\n"
    )

    # 4. 待观察的增量对话切片 (Incremental Units) 与 5. 继承段 (Inherited Units)
    if session_input.is_subagent and session_input.inherited_units:
        inherited_json = format_units_for_observer(session_input.inherited_units)
        inc_json = format_units_for_observer(session_input.incremental_units)
        parts.append(
            "## 3. 会话消息流水（含分叉背景与增量执行）\n"
            "### (1) 上层继承背景消息 (Inherited Units - 仅作为事实与硬约束参考，勿当作当前 Agent 的产物):\n"
            f"```json\n{inherited_json}\n```\n\n"
            "### (2) 目标 Agent 增量对话切片 (Incremental Units - 本次重点观察与提炼范围):\n"
            f"```json\n{inc_json}\n```\n"
        )
    else:
        inc_json = format_units_for_observer(session_input.incremental_units)
        parts.append(
            "## 3. 待观察的增量对话切片 (Incremental Units)\n"
            f"以下是上一断点以来的新增对话。每个 Unit 是一个原子切片，其末尾标有合法断点 seq (本次最高目标断点为 seq={session_input.target_seq})：\n"
            f"```json\n{inc_json}\n```\n"
        )

    parts.append(
        "---\n"
        "请通读上述内容，调用专属 Note 工具维护 11 张 Note 业务表。\n"
        "- 注意：对于切片中程序底层已自动插入 auto_record 的物理资源（如 artifact_id、relationship_id 等），必须在提交断点前完成任务归属绑定或清理。\n"
        "- 观察与整理完成后，必须调用 `note_update_end(covered_until_seq=...)` 提交合法断点并安全退出。"
    )

    return "\n\n".join(parts)


class TaskNoteObserverScheduler:
    """Task Note 观察调度器。"""

    def __init__(
        self,
        *,
        observer_runner: Callable[[ObserverSessionInput], Awaitable[ObserverSessionResult]],
        slicer: TaskNoteUnitSlicer | None = None,
        store: TaskNoteStore | None = None,
        prompt_provider: TaskNotePromptProvider | None = None,
        trigger_threshold_tokens: int = DEFAULT_OBSERVER_TRIGGER_TOKENS,
    ) -> None:
        if observer_runner is None:
            raise ValueError("observer_runner 为必填参数，严禁无 runner 或模拟路径 (D02)")
        self._observer_runner = observer_runner
        self.slicer = slicer or TaskNoteUnitSlicer(trigger_threshold_tokens=trigger_threshold_tokens)
        self.store = store or get_task_note_store()
        self.prompt_provider = prompt_provider or TaskNotePromptProvider()
        self._running_tasks: dict[str, asyncio.Task[ObserverSessionResult]] = {}
        self._latest_results: dict[str, ObserverSessionResult] = {}

    def is_observer_running(self, agent_instance_id: str) -> bool:
        """检查目标业务 Agent 是否已有观察者正在后台运行。"""
        task = self._running_tasks.get(agent_instance_id)
        if task is None:
            return False
        return not task.done()

    def get_running_observer_count(self) -> int:
        """获取当前后台正在运行的观察者数量。"""
        return sum(1 for t in self._running_tasks.values() if not t.done())

    def get_latest_result(self, agent_instance_id: str) -> ObserverSessionResult | None:
        return self._latest_results.get(agent_instance_id)

    def start(
        self,
        ticket: ObserveTicket,
        *,
        target_agent_definition_prompt: str | None = None,
        parent_agent_instance_id: str | None = None,
        ancestor_chain: list[str] | None = None,
        scene_id: str | None = None,
    ) -> asyncio.Task[ObserverSessionResult]:
        """为已获取 ticket 的观察创建并启动异步后台任务。不进行 await。"""
        async def _execute_task() -> ObserverSessionResult:
            # 1. 增量与继承段切片
            slice_res = self.slicer.slice_messages(
                ticket.messages,
                start_after_message_id=ticket.start_after_message_id,
                end_message_id=ticket.end_message_id,
                snapshot_message_id=ticket.snapshot_message_id,
                inherited_until_message_id=ticket.inherited_until_message_id,
            )

            # 2. 异步在线程池读取快照，避免阻塞主循环
            snapshot = await asyncio.to_thread(
                self.store.get_note_snapshot,
                task_id=ticket.task_id,
                agent_instance_id=ticket.agent_instance_id,
            )

            # 3. 组装 ObserverSessionInput
            session_input = ObserverSessionInput(
                task_id=ticket.task_id,
                agent_instance_id=ticket.agent_instance_id,
                run_id=ticket.run_id,
                conversation_id=ticket.conversation_id,
                target_seq=slice_res.max_seq,
                note_snapshot=snapshot,
                incremental_units=slice_res.incremental_units,
                inherited_units=slice_res.inherited_units,
                is_subagent=ticket.is_subagent,
                parent_agent_instance_id=parent_agent_instance_id,
                ancestor_chain=ancestor_chain or [],
                target_agent_definition_prompt=target_agent_definition_prompt,
                scene_id=scene_id,
            )

            return await self._run_observer_session(session_input)

        task = asyncio.create_task(_execute_task())
        self._running_tasks[ticket.agent_instance_id] = task
        logger.info(
            "已为业务 Agent %s 启动后台观察任务: run_id=%s",
            ticket.agent_instance_id,
            ticket.run_id,
        )
        return task

    async def run_sync(
        self,
        ticket: ObserveTicket,
        *,
        target_agent_definition_prompt: str | None = None,
        parent_agent_instance_id: str | None = None,
        ancestor_chain: list[str] | None = None,
        scene_id: str | None = None,
    ) -> ObserverSessionResult:
        """同步启动并等待观察会话完成。"""
        task = self.start(
            ticket,
            target_agent_definition_prompt=target_agent_definition_prompt,
            parent_agent_instance_id=parent_agent_instance_id,
            ancestor_chain=ancestor_chain,
            scene_id=scene_id,
        )
        return await task

    async def cancel(self, agent_instance_id: str) -> None:
        """取消指定 Agent 实例正在运行的观察任务，吞掉 CancelledError。"""
        task = self._running_tasks.get(agent_instance_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_observer_session(self, session_input: ObserverSessionInput) -> ObserverSessionResult:
        """执行单个观察者会话全生命周期并收尾。"""
        agent_id = session_input.agent_instance_id
        try:
            coordinator = get_task_note_tracking_coordinator()
        except RuntimeError:
            coordinator = None
        result: ObserverSessionResult | None = None
        try:
            result = await self._observer_runner(session_input)
            self._latest_results[agent_id] = result
            return result
        except asyncio.CancelledError:
            logger.info("观察者任务被取消: agent_instance_id=%s, run_id=%s", agent_id, session_input.run_id)
            raise
        except Exception as exc:
            logger.error("观察者会话执行发生未捕获异常: %s", exc, exc_info=True)
            result = ObserverSessionResult(success=False, error_message=str(exc))
            self._latest_results[agent_id] = result
            return result
        finally:
            self._running_tasks.pop(agent_id, None)
            # runner 返回 success=False、抛异常或被取消时，在 finally 里通知 coordinator.complete(success=False)
            # success=True 时 runner 已经调用过 complete，这里不再调用（complete 幂等）
            if result is None or not result.success:
                try:
                    if coordinator:
                        await coordinator.complete(
                            instance_id=agent_id,
                            conversation_id=session_input.conversation_id,
                            run_id=session_input.run_id,
                            success=False,
                        )
                except Exception as ce:
                    logger.warning("观察者异常收尾通知 coordinator 失败: %s", ce)
