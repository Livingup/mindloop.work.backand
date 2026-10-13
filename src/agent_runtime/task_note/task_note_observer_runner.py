# -*- coding: utf-8 -*-
"""Task Note 观察执行器 (TaskNoteObserverRunner)。

核心职责：
1. 实现异步调用接口: async __call__(session_input: ObserverSessionInput) -> ObserverSessionResult;
2. 每次观察新建独立 Conversation，装配系统提示词 (system_note_update) 与首条 user 消息 (D03 payload);
3. 挂载 36 个 Note 工具，注入 ObserverBinding 上下文；
4. 通过 llm_request_service 驱动与 LLM 的多轮交互 (单步循环最多 30 轮)，直到 note_update_end 返回 complete 或达到轮数上限；
5. Note 工具调用成功与 watermark 推进后，对外发布 task_note.updated 事件 (接收方为目标业务 Agent)；
6. 异常或失败时安全返回 ObserverSessionResult(success=False, ...)，不抛出阻断异常。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable
import uuid

from agent_runtime.communication.runtime_event_publisher import (
    RuntimeCommunicationContext,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    TextPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.llm_client.llm_request_service import LlmPromptRequest
from agent_runtime.task_note.task_note_observer_binding import (
    ObserverBinding,
    reset_current_observer_binding,
    set_current_observer_binding,
)
from agent_runtime.task_note.task_note_observer_scheduler import (
    ObserverSessionInput,
    ObserverSessionResult,
    build_observer_user_payload,
)
from agent_runtime.task_note.task_note_prompt_provider import TaskNotePromptProvider
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    ToolRuntimeContext,
)

logger = logging.getLogger("agent_runtime.task_note.observer_runner")


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _tool_use_ref(tool_use: ToolUsePart) -> str:
    return tool_use.tool_use_id or tool_use.name


class TaskNoteObserverRunner:
    """Task Note 真实观察会话执行器 (D02)。"""

    def __init__(
        self,
        *,
        llm_request_service: Any,
        prompt_provider: TaskNotePromptProvider | None = None,
        local_tool_gateway: Any | None = None,
        llm_registry_gateway: Any | None = None,
        runtime_gateway: Any | None = None,
        event_publisher: Any | None = None,
        max_turns: int = 30,
        observer_min_advance_ratio: float = 0.5,
    ) -> None:
        if llm_request_service is None:
            raise ValueError("llm_request_service 不能为空")
        self._llm_request_service = llm_request_service
        self._prompt_provider = prompt_provider or TaskNotePromptProvider(local_tool_gateway=local_tool_gateway)
        self._local_tool_gateway = local_tool_gateway
        self._llm_registry_gateway = llm_registry_gateway
        self._runtime_gateway = runtime_gateway
        self._event_publisher = event_publisher
        self._max_turns = max_turns
        self._observer_min_advance_ratio = observer_min_advance_ratio

    def _resolve_model(self, agent_instance_id: str) -> Any:
        """解析目标 Agent 对应的 LLM 模型配置。"""
        if self._runtime_gateway is not None and self._llm_registry_gateway is not None:
            try:
                instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
                if instance is not None:
                    if getattr(instance, "llm_policy_group_id", None):
                        policy = self._llm_registry_gateway.resolve_execution_policy(instance.llm_policy_group_id)
                        if policy and policy.candidates:
                            return policy.candidates[0].resolved_model
                    if getattr(instance, "llm_provider_id", None):
                        return self._llm_registry_gateway.resolve_provider_model(
                            instance.llm_provider_id,
                            getattr(instance, "llm_model_id", None),
                        )
            except Exception as exc:
                logger.debug("从 runtime/registry 解析目标 Agent 模型异常: %s", exc)

        if self._llm_registry_gateway is not None:
            try:
                # 尝试获取默认/首个可用策略
                policies = self._llm_registry_gateway.list_execution_policies()
                if policies:
                    pol = self._llm_registry_gateway.resolve_execution_policy(policies[0].policy_id)
                    if pol and pol.candidates:
                        return pol.candidates[0].resolved_model
            except Exception as exc:
                logger.debug("获取默认执行策略异常: %s", exc)

        # 兜底构造轻量 Resolved 模型对象
        class _FallbackResolvedModel:
            provider_id = "default"
            binding_id = "default"
            model_id = "default"
            api_format = "openai"
            supported_input_modalities = ("text",)

        return _FallbackResolvedModel()

    async def _execute_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        runtime_ctx: ToolRuntimeContext,
    ) -> BaseToolResult:
        """执行单个 Note 工具调用。"""
        if self._local_tool_gateway is not None:
            try:
                return await self._local_tool_gateway.execute_tool(
                    tool_name,
                    tool_args,
                    runtime_context=runtime_ctx,
                )
            except Exception as exc:
                logger.warning("LocalToolGateway 执行 %s 异常: %s，回退直接调用", tool_name, exc)

        # 直接调用 task_note_tool 中的对应函数
        from agent_runtime.task_note import task_note_tool
        func = getattr(task_note_tool, tool_name, None)
        if func is None or not callable(func):
            return BaseToolResult(
                success=False,
                execution_status="failed",
                error_code="ToolNotFound",
                error_message=f"未找到 Note 工具: {tool_name}",
            )

        # 尝试通过参数类型实例化 Pydantic Request
        try:
            import inspect
            sig = inspect.signature(func)
            req_cls = None
            for p in sig.parameters.values():
                if p.name == "request" and p.annotation is not inspect.Parameter.empty:
                    req_cls = p.annotation
                    break
            if req_cls is not None and hasattr(req_cls, "model_validate"):
                req_obj = req_cls.model_validate(tool_args)
                res = func(req_obj, runtime_ctx)
            else:
                res = func(tool_args, runtime_ctx)

            if asyncio.iscoroutine(res):
                res = await res
            return res
        except Exception as exc:
            logger.error("直接调用 Note 工具 %s 失败: %s", tool_name, exc)
            return BaseToolResult(
                success=False,
                execution_status="failed",
                error_code="ExecutionError",
                error_message=str(exc),
                output={"error": str(exc)},
            )

    async def __call__(self, session_input: ObserverSessionInput) -> ObserverSessionResult:
        """执行独立观察会话。"""
        target_agent_id = session_input.agent_instance_id
        task_id = session_input.task_id
        scene_id = session_input.scene_id or f"scene-{task_id}"

        resolved_model = self._resolve_model(target_agent_id)
        api_format = getattr(resolved_model, "api_format", "openai")

        # 1. 构建全新独立 Conversation
        conv_id = f"conv_obs_{uuid.uuid4().hex[:12]}"
        conversation = Conversation(id=conv_id)

        # 2. 组装系统提示词 (正本模板渲染结果)
        try:
            system_prompt = self._prompt_provider.render_note_system_prompt(
                agent_instance_id=target_agent_id,
            )
        except Exception as exc:
            logger.error("渲染观察者系统提示词失败: %s", exc)
            return ObserverSessionResult(success=False, error_message=f"渲染系统提示词失败: {exc}")

        # 3. 获取 36 个 Note 工具定义
        llm_tools = ()
        if self._local_tool_gateway is not None:
            try:
                llm_tools = self._prompt_provider.get_note_tools(api_format=api_format)
            except Exception as exc:
                logger.warning("获取 Note 工具 LlmToolDefinition 异常: %s", exc)

        # 4. 组装首条 User 消息 (D03 payload)
        user_payload = build_observer_user_payload(
            session_input,
            target_agent_definition_prompt=session_input.target_agent_definition_prompt or "",
        )
        user_msg = UniMessage(
            role=ROLE_USER,
            content=[TextPart(text=user_payload)],
            message_id=f"msg_obs_user_{uuid.uuid4().hex[:8]}",
        )
        conversation.append_message(user_msg)

        # 5. 绑定上下文 ObserverBinding (M5)
        binding = ObserverBinding(
            task_id=task_id,
            target_agent_instance_id=target_agent_id,
            scene_id=scene_id,
            units=session_input.incremental_units,
            observer_min_advance_ratio=self._observer_min_advance_ratio,
        )
        token = set_current_observer_binding(binding)

        runtime_ctx = ToolRuntimeContext(
            task_id=task_id,
            agent_instance_id=target_agent_id,
            scene_id=scene_id,
        )

        tools_executed_count = 0
        turns = 0

        try:
            while turns < self._max_turns:
                turns += 1
                try:
                    prompt_req = LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved_model,
                        system_prompt=system_prompt,
                        tools=list(llm_tools) if llm_tools else None,
                    )
                    prompt_res = await self._llm_request_service.send_prompt(prompt_req)
                except Exception as exc:
                    logger.error("观察者会话 LLM 调用异常 (turn=%d): %s", turns, exc)
                    return ObserverSessionResult(
                        success=False,
                        error_message=f"LLM 调用失败: {exc}",
                        tools_executed_count=tools_executed_count,
                    )

                assistant_msg = prompt_res.assistant_message
                conversation.append_message(assistant_msg)

                tool_uses = _extract_tool_uses(assistant_msg)
                if not tool_uses:
                    # 若没有工具调用，提示继续调用工具
                    if turns >= self._max_turns:
                        break
                    reminder_msg = UniMessage(
                        role=ROLE_USER,
                        content=[TextPart(text="请调用相应的 Note 工具维护知识库，最后必须调用 note_update_end 提交断点。")],
                        message_id=f"msg_obs_remind_{uuid.uuid4().hex[:8]}",
                    )
                    conversation.append_message(reminder_msg)
                    continue

                # 执行工具调用批次
                result_parts: list[ToolResultPart] = []
                completed_by_note_update_end = False
                completed_covered_seq = 0
                completed_note_version = 0

                for tu in tool_uses:
                    call_ref = _tool_use_ref(tu)
                    t_name = tu.name
                    t_args = tu.arguments if hasattr(tu, "arguments") and tu.arguments is not None else getattr(tu, "args", {}) or {}

                    tool_res: BaseToolResult = await self._execute_tool_call(
                        tool_name=t_name,
                        tool_args=t_args,
                        runtime_ctx=runtime_ctx,
                    )
                    tools_executed_count += 1

                    # 检查是否成功并发布 task_note.updated 事件 (4.10)
                    if tool_res.success and self._event_publisher is not None:
                        try:
                            comm_ctx = RuntimeCommunicationContext(
                                stream_id=f"agent.task.{task_id}",
                                trace_id=None,
                                metadata={
                                    "task_id": task_id,
                                    "scene_id": scene_id,
                                    "agent_instance_id": target_agent_id,
                                },
                            )
                            await self._event_publisher.publish(
                                comm_ctx,
                                "task_note.updated",
                                {
                                    "task_id": task_id,
                                    "agent_instance_id": target_agent_id,
                                    "scene_id": scene_id,
                                    "tool_name": t_name,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                                source="task_note_observer_runner",
                            )
                        except Exception as e:
                            logger.warning("发布 task_note.updated 事件失败 (静默容错): %s", e)

                    # 检查 note_update_end 决策
                    if t_name == "note_update_end" and tool_res.success:
                        decision = getattr(tool_res.loop_control, "decision", None) if tool_res.loop_control else None
                        if decision == "complete" or (tool_res.output and tool_res.output.get("covered_until_seq", 0) > 0):
                            completed_by_note_update_end = True
                            completed_covered_seq = tool_res.output.get("covered_until_seq", 0)
                            completed_note_version = tool_res.output.get("note_version", 1)

                    output_payload = tool_res.output if tool_res.output is not None else {"message": tool_res.error_message or "ok"}
                    result_parts.append(
                        ToolResultPart(
                            tool_use_ref=call_ref,
                            is_error=not tool_res.success,
                            output=output_payload,
                        )
                    )

                # 将工具返回作为 tool message 追加到观察会话
                tool_msg = UniMessage(
                    role=ROLE_TOOL,
                    content=result_parts,
                    message_id=f"msg_obs_tool_{uuid.uuid4().hex[:8]}",
                )
                conversation.append_message(tool_msg)

                if completed_by_note_update_end:
                    logger.info(
                        "观察者会话成功完成 note_update_end: target_agent=%s, seq=%d, version=%d, tools_count=%d",
                        target_agent_id,
                        completed_covered_seq,
                        completed_note_version,
                        tools_executed_count,
                    )
                    return ObserverSessionResult(
                        success=True,
                        covered_until_seq=completed_covered_seq,
                        note_version=completed_note_version,
                        tools_executed_count=tools_executed_count,
                    )

            # 超出最大轮数
            logger.warning("观察者会话已达到最大轮数 %d，未能提交合法 note_update_end", self._max_turns)
            return ObserverSessionResult(
                success=False,
                error_message=f"超过最大观察轮数 ({self._max_turns})，未能成功提交 note_update_end",
                tools_executed_count=tools_executed_count,
            )

        finally:
            reset_current_observer_binding(token)
