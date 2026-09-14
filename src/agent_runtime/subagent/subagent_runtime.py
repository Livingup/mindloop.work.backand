# -*- coding: utf-8 -*-
"""本地子 Agent 协作运行时门面。"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING, PendingUserMessageAttachment
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.agent.file_input_policy import (
    FileInputCandidate,
    default_file_input_limits,
    validate_file_input_candidates,
)
from agent_runtime.agent.north_gateway.agent_runtime_contracts import CreateChildAgentInstanceRequest
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import (
    ConfigurationComponentEvent,
    ROLE_USER,
    TextPart,
    TextPartMeta,
    UniMessage,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.file_resource.north_gateway.resource_create_contracts import RemoteUrlCreateRequest
from agent_runtime.file_resource.shared_service import get_file_resource_service
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF

if TYPE_CHECKING:
    from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
    from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import AgentDefinitionNorthGateway
    from agent_runtime.conversation_message.conversation_repository import ConversationRepository
    from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway
    from agent_runtime.communication.runtime_event_publisher import RuntimeEventPublisher

logger = logging.getLogger(__name__)

SUBAGENT_SUPPLEMENTARY_PROMPT_TEMPLATE = (
    "你是由{{父agent实例id}}创建的子agent，上面的对话是父agent在创建你时之前的对话过程，"
    "仅作为背景知识参考，你只需要完成父agent给你派发的子任务内容，不要去完成父agent的任务内容，"
    "子任务内容会在下面的对话消息中给到你。注意一定不要把自己当成父agent去完成父agent的任务！"
    "注意你的任务边界！你现在可用的工具范围是{{available_tools}}"
)


def _extract_definition_tool_names(definition: Any) -> set[str]:
    if definition is None:
        return set()
    tool_names = set(getattr(definition, "tool_ids", ()))
    for mcp in getattr(definition, "mcp_tools", ()):
        tool_names.update(getattr(mcp, "tool_ids", ()))
    return tool_names


def _extract_tool_names_from_tools(tools: Any) -> set[str]:
    if not tools:
        return set()
    names: set[str] = set()
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
            if name:
                names.add(str(name))
        elif hasattr(t, "name"):
            names.add(str(t.name))
    return names



class SubagentRuntime:
    """本地子 Agent 工具的运行时编排门面。

    该对象隔离本地工具函数和执行引擎/运行态网关，保证工具本身只表达父子 Agent 协作语义，
    具体创建、投递、状态查询和释放动作都通过既有运行时网关与执行引擎完成。
    """

    def __init__(
        self,
        *,
        runtime_gateway: "AgentRuntimeGateway",
        agent_definition_gateway: "AgentDefinitionNorthGateway",
        llm_registry_gateway: "LlmModelRegistryGateway",
        execution_engine: "AgentExecutionEngine",
        event_publisher: "RuntimeEventPublisher",
        conversation_repository: "ConversationRepository",
    ) -> None:
        self._runtime_gateway = runtime_gateway
        self._agent_definition_gateway = agent_definition_gateway
        self._llm_registry_gateway = llm_registry_gateway
        self._execution_engine = execution_engine
        self._event_publisher = event_publisher
        self._conversation_repository = conversation_repository

    async def create_subagent(
        self,
        *,
        parent_instance_id: str,
        agent_definition_id: str,
        llm_policy_group_id: str | None = None,
        initial_message: str | None = None,
        initial_message_attachments: list[dict[str, Any]] | None = None,
        parent_conversation_id: str | None = None,
        conversation_history_copy_message_count: int | None = None,
    ) -> dict[str, Any]:
        parent = self._runtime_gateway.get_agent_instance(parent_instance_id)
        task = self._runtime_gateway.get_task(parent.task_id)
        if task.is_terminal:
            raise ValueError(f"当前任务已结束，不能创建子 Agent: {task.task_id}")
        parent_definition = self._agent_definition_gateway.get_agent_definition(parent.agent_definition_id)
        if agent_definition_id not in parent_definition.available_sub_agent_ids:
            raise PermissionError(
                f"父 Agent 定义未授权创建该子 Agent: {parent.agent_definition_id}/{agent_definition_id}"
            )
        child_definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if child_definition.disabled:
            raise ValueError(f"子 Agent 定义已禁用: {agent_definition_id}")
        initial_attachments = await self._resolve_url_attachments(initial_message_attachments or [])
        if initial_attachments:
            validate_file_input_candidates(_file_input_candidates(initial_attachments))
        selection = self._resolve_child_llm_selection(
            parent_llm_provider_id=parent.llm_provider_id,
            parent_llm_model_binding_id=parent.llm_model_binding_id,
            parent_llm_policy_group_id=parent.llm_policy_group_id,
            child_agent_definition_id=agent_definition_id,
            requested_policy_group_id=llm_policy_group_id,
            default_policy_group_id=child_definition.default_llm_policy_group_id,
            allowed_policy_group_ids=child_definition.allowed_llm_policy_group_ids,
        )
        child_thread_id = f"thread-{uuid4().hex}"
        child_instance_id = f"agent-instance-{uuid4().hex}"
        parent_name = parent_definition.name or parent_definition.agent_id
        seeded_conversation_id = await self._seed_child_conversation_from_parent_prefix(
            parent=parent,
            parent_agent_name=parent_name,
            child_instance_id=child_instance_id,
            child_thread_id=child_thread_id,
            child_agent_definition_id=agent_definition_id,
            parent_conversation_id=parent_conversation_id,
            conversation_history_copy_message_count=conversation_history_copy_message_count,
            parent_definition=parent_definition,
            child_definition=child_definition,
        )
        try:
            _updated_task, child, relation = self._runtime_gateway.create_child_agent_instance(
                CreateChildAgentInstanceRequest(
                    task_id=task.task_id,
                    parent_instance_id=parent.instance_id,
                    thread_id=child_thread_id,
                    current_working_directory=parent.current_working_directory,
                    agent_definition_id=agent_definition_id,
                    llm_provider_id=selection["llm_provider_id"],
                    llm_model_binding_id=selection["llm_model_binding_id"],
                    llm_policy_group_id=selection.get("llm_policy_group_id"),
                    child_instance_id=child_instance_id,
                    conversation_id=seeded_conversation_id,
                )
            )
        except Exception:
            if seeded_conversation_id is not None:
                try:
                    await self._conversation_repository.delete(seeded_conversation_id)
                except Exception:  # noqa: BLE001 - creation failure cleanup must not hide root cause
                    from agent_runtime.codex_oauth.diagnostics import stable_fingerprint
                    logger.exception(
                        "清理子 Agent 预创建会话失败: conversation_fingerprint=%s",
                        stable_fingerprint(seeded_conversation_id),
                    )
                else:
                    try:
                        from agent_runtime.codex_oauth.factory import CodexOAuthFactory
                        codex_service = CodexOAuthFactory.create_service()
                        if codex_service.opened:
                            await codex_service.delete_session_binding(seeded_conversation_id)
                    except Exception as cleanup_exc:  # noqa: BLE001 - binding cleanup cannot hide root cause
                        from agent_runtime.codex_oauth.diagnostics import stable_fingerprint
                        logger.warning(
                            "清理已永久删除会话的 Codex session binding 失败: conversation_fingerprint=%s "
                            "error_type=%s error_code=%s",
                            stable_fingerprint(seeded_conversation_id),
                            cleanup_exc.__class__.__name__,
                            getattr(cleanup_exc, "code", None),
                        )
            raise
        await self._publish_subagent_created(
            parent_instance_id=parent.instance_id,
            child_instance_id=child.instance_id,
            relation=relation,
        )
        if (initial_message and initial_message.strip()) or initial_attachments:
            await self._send_agent_message(
                from_instance_id=parent.instance_id,
                to_instance_id=child.instance_id,
                content=initial_message or "",
                attachments=initial_attachments,
            )
        return {
            "status": "created",
            "parent_agent_instance_id": parent.instance_id,
            "child_agent_instance_id": child.instance_id,
            "child_agent_definition_id": child.agent_definition_id,
            "task_id": child.task_id,
            "thread_id": child.thread_id,
            "relation_id": relation.relation_id,
            "llm_policy_group_id": child.llm_policy_group_id,
            "llm_provider_id": child.llm_provider_id,
            "llm_model_binding_id": child.llm_model_binding_id,
        }

    async def send_agent_message(
        self,
        *,
        from_instance_id: str,
        to_instance_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved_attachments = await self._resolve_url_attachments(attachments or [])
        return await self._send_agent_message(
            from_instance_id=from_instance_id,
            to_instance_id=to_instance_id,
            content=content,
            attachments=resolved_attachments,
        )

    async def _send_agent_message(
        self,
        *,
        from_instance_id: str,
        to_instance_id: str,
        content: str,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
    ) -> dict[str, Any]:
        content = content.strip()
        if not content and not attachments:
            raise ValueError("content 和 attachments 必须至少提供一个。")
        source = self._runtime_gateway.get_agent_instance(from_instance_id)
        target = self._runtime_gateway.get_agent_instance(to_instance_id)
        task = self._runtime_gateway.get_task(source.task_id)
        if source.task_id != target.task_id:
            raise PermissionError("只能在同一任务内向父/子 Agent 发送消息。")
        if task.is_terminal:
            raise ValueError(f"当前任务已结束，不能发送子 Agent 消息: {task.task_id}")
        if not self._can_message(task_id=task.task_id, from_instance_id=source.instance_id, to_instance_id=target.instance_id):
            raise PermissionError("只能在未释放的直接父子 Agent 之间发送消息。")
        source_definition = self._agent_definition_gateway.get_agent_definition(source.agent_definition_id)
        source_name = source_definition.name or source_definition.agent_id
        user_text = f"【@本地Agent {source_name} {source.instance_id}】{content}"
        metadata = {
            "source_type": "local_agent",
            "transport": "subagent",
            "channel": "subagent",
            "display_text": content,
            "text_format": "markdown",
            "attachments": _attachments_metadata(attachments),
            "local_agent": {
                "name": source_name,
                "agent_definition_id": source.agent_definition_id,
                "instance_id": source.instance_id,
            },
            "subagent": {
                "from_instance_id": source.instance_id,
                "to_instance_id": target.instance_id,
                "task_id": task.task_id,
            },
        }
        result = await self._execution_engine.submit_agent_instance_message(
            agent_instance_id=target.instance_id,
            user_text=user_text,
            attachments=attachments,
            message_metadata=metadata,
        )
        return {
            "status": result.status,
            "task_id": result.task_id,
            "target_agent_instance_id": result.instance_id,
            "pending_message_id": result.pending_message_id,
            "queued_for_later": result.queued_for_later,
            "delivery": "accepted",
        }

    async def _resolve_url_attachments(
        self,
        attachments: list[dict[str, Any]],
    ) -> tuple[PendingUserMessageAttachment, ...]:
        if not attachments:
            return ()
        requests: list[RemoteUrlCreateRequest] = []
        expected_kinds: list[str] = []
        names: list[str | None] = []
        for index, attachment in enumerate(attachments):
            kind = _normalize_attachment_kind(attachment.get("kind"), index)
            url = _normalize_attachment_url(attachment.get("url"), index)
            mime_type = _normalize_attachment_mime_type(attachment.get("mime_type"))
            requests.append(
                RemoteUrlCreateRequest(
                    resource_url=url,
                    resource_mime_type=mime_type,
                    max_bytes=_attachment_download_limit(kind),
                )
            )
            expected_kinds.append(kind)
            names.append(_normalize_optional_text(attachment.get("name")))
        service = get_file_resource_service()
        created = await monitored_to_thread(
            service.create_resources_from_remote_urls,
            requests,
            operation="subagent.resolve_url_attachments.create_resources",
            caller="SubagentRuntime._resolve_url_attachments",
            pool=BlockingPool.FILE_RESOURCE,
            log=logger,
            diagnostic_context={
                "attachment_count": len(attachments),
                "request_count": len(requests),
            },
        )
        resources = await monitored_to_thread(
            service.get_resources,
            [item.resource_id for item in created],
            operation="subagent.resolve_url_attachments.get_resources",
            caller="SubagentRuntime._resolve_url_attachments",
            pool=BlockingPool.FILE_RESOURCE,
            log=logger,
            diagnostic_context={
                "attachment_count": len(attachments),
                "resource_count": len(created),
            },
        )
        resolved: list[PendingUserMessageAttachment] = []
        for index, resource in enumerate(resources):
            if resource is None:
                raise ValueError("URL 文件资源创建后无法读取资源详情。")
            metadata = resource.resource_metadata
            actual_kind = metadata.resource_category
            if actual_kind != expected_kinds[index]:
                raise ValueError(
                    f"附件类型与 URL 文件实际类型不一致: expected={expected_kinds[index]}, actual={actual_kind}"
                )
            resolved.append(
                PendingUserMessageAttachment(
                    kind=actual_kind,
                    resource_id=resource.resource_id,
                    name=names[index],
                    mime_type=resource.resource_mime_type,
                    size_in_bytes=resource.resource_size_in_bytes,
                    width=getattr(metadata, "image_width", None),
                    height=getattr(metadata, "image_height", None),
                    page_count=getattr(metadata, "pdf_page_count", None),
                )
            )
        return tuple(resolved)

    def get_agent_status(self, requester_instance_id: str, target_instance_id: str) -> dict[str, Any]:
        requester = self._runtime_gateway.get_agent_instance(requester_instance_id)
        target = self._runtime_gateway.get_agent_instance(target_instance_id)
        if requester.task_id != target.task_id:
            raise PermissionError("只能查询同一任务内的 Agent 状态。")
        task = self._runtime_gateway.get_task(requester.task_id)
        if target.instance_id != requester.instance_id and not self._is_direct_parent_child(task.task_id, requester.instance_id, target.instance_id):
            raise PermissionError("只能查询当前 Agent 自身或直接父子 Agent 的状态。")
        relation = task.child_relation_of(target.instance_id)
        released = bool(relation and relation.is_released)
        if task.is_terminal or released:
            public_status = "ended"
        elif target.status == AGENT_INSTANCE_STATUS_RUNNING:
            public_status = "running"
        else:
            public_status = "paused"
        return {
            "agent_instance_id": target.instance_id,
            "agent_definition_id": target.agent_definition_id,
            "task_id": target.task_id,
            "status": public_status,
            "runtime_status": target.status,
            "released": released,
            "release_reason": relation.release_reason if relation else None,
        }

    def list_subagents(self, parent_instance_id: str) -> list[dict[str, Any]]:
        children = self._runtime_gateway.list_unreleased_child_agent_instances(parent_instance_id)
        return [
            {
                "child_agent_instance_id": child.instance_id,
                "child_agent_definition_id": child.agent_definition_id,
                "task_id": child.task_id,
                "status": "running" if child.status == AGENT_INSTANCE_STATUS_RUNNING else "paused",
                "runtime_status": child.status,
            }
            for child in children
        ]

    async def end_subagent(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self.end_subagents(
            parent_instance_id=parent_instance_id,
            child_instance_ids=[child_instance_id],
            reason=reason,
        )

    async def end_subagents(
        self,
        *,
        parent_instance_id: str,
        child_instance_ids: list[str],
        reason: str,
    ) -> dict[str, Any]:
        normalized_child_ids = tuple(dict.fromkeys(child_id.strip() for child_id in child_instance_ids if child_id.strip()))
        if not normalized_child_ids:
            raise ValueError("child_agent_instance_ids 至少提供一个。")
        result_lines: list[str] = []
        all_released_ids: list[str] = []
        success_count = 0
        failed_count = 0
        task_id: str | None = None
        eligibility_by_child: dict[str, tuple[bool, str | None]] = {}
        for child_instance_id in normalized_child_ids:
            is_releasable, error_message, child_task_id = self._validate_end_subagent_target(
                parent_instance_id=parent_instance_id,
                child_instance_id=child_instance_id,
            )
            if child_task_id is not None:
                task_id = task_id or child_task_id
            eligibility_by_child[child_instance_id] = (is_releasable, error_message)
        for child_instance_id in normalized_child_ids:
            is_releasable, error_message = eligibility_by_child[child_instance_id]
            if not is_releasable:
                failed_count += 1
                result_lines.append(
                    _end_subagent_result_line(
                        child_instance_id,
                        succeeded=False,
                        error_message=error_message or f"子 Agent 释放失败: {child_instance_id}",
                    )
                )
                continue
            try:
                updated_task, released_instances = self._runtime_gateway.release_child_agent_instance(
                    parent_instance_id,
                    child_instance_id,
                    reason=reason,
                )
            except (KeyError, PermissionError, ValueError) as exc:
                failed_count += 1
                result_lines.append(
                    _end_subagent_result_line(
                        child_instance_id,
                        succeeded=False,
                        error_message=str(exc),
                    )
                )
                continue
            released_ids = [instance.instance_id for instance in released_instances]
            task_id = updated_task.task_id
            all_released_ids.extend(released_ids)
            success_count += 1
            result_lines.append(_end_subagent_result_line(child_instance_id, succeeded=True))
            await self._publish_subagent_released(
                parent_instance_id=parent_instance_id,
                child_instance_id=child_instance_id,
                released_agent_instance_ids=released_ids,
                task=updated_task,
                reason=reason,
            )
        return {
            "result_text": _end_subagent_result_text(
                success_count=success_count,
                failed_count=failed_count,
                result_lines=result_lines,
            ),
            "success_count": success_count,
            "failed_count": failed_count,
            "released_agent_instance_ids": all_released_ids,
            "task_id": task_id,
            "parent_agent_instance_id": parent_instance_id,
            "reason": reason,
        }

    def _validate_end_subagent_target(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
    ) -> tuple[bool, str | None, str | None]:
        try:
            parent = self._runtime_gateway.get_agent_instance(parent_instance_id)
        except (KeyError, ValueError) as exc:
            return False, str(exc), None
        try:
            child = self._runtime_gateway.get_agent_instance(child_instance_id)
        except (KeyError, ValueError) as exc:
            return False, str(exc), parent.task_id
        if parent.task_id != child.task_id:
            return False, "父 Agent 与子 Agent 不属于同一任务。", parent.task_id
        try:
            task = self._runtime_gateway.get_task(parent.task_id)
        except KeyError as exc:
            return False, str(exc), parent.task_id
        relation = task.child_relation_of(child_instance_id)
        if relation is None:
            return False, f"子 Agent 实例不属于任务父子关系树: {child_instance_id}", task.task_id
        if relation.parent_instance_id != parent_instance_id:
            return False, "只能结束当前 Agent 直接创建且尚未释放的子 Agent", task.task_id
        if relation.is_released:
            return False, f"子 Agent 已释放: {child_instance_id}", task.task_id
        running_instance_id = self._find_running_instance_in_release_subtree(task, child_instance_id)
        if running_instance_id is not None:
            return False, _running_subagent_error_message(child_instance_id, running_instance_id), task.task_id
        return True, None, task.task_id

    def _find_running_instance_in_release_subtree(
        self,
        task: AgentTask,
        child_instance_id: str,
    ) -> str | None:
        stack = [child_instance_id]
        visited: set[str] = set()
        while stack:
            current_id = stack.pop()
            if current_id in visited:
                continue
            visited.add(current_id)
            try:
                instance = self._runtime_gateway.get_agent_instance(current_id)
            except KeyError:
                return None
            if instance.status == AGENT_INSTANCE_STATUS_RUNNING:
                return current_id
            stack.extend(reversed(task.unreleased_children_of(current_id)))
        return None

    async def _seed_child_conversation_from_parent_prefix(
        self,
        *,
        parent: Any,
        parent_agent_name: str,
        child_instance_id: str,
        child_thread_id: str,
        child_agent_definition_id: str,
        parent_conversation_id: str | None,
        conversation_history_copy_message_count: int | None,
        parent_definition: Any = None,
        child_definition: Any = None,
    ) -> str | None:
        if parent_conversation_id is None or conversation_history_copy_message_count is None:
            logger.info(
                "创建子 Agent 未继承父会话历史: parent_instance_id=%s parent_conversation_id=%s copy_count=%s",
                parent.instance_id,
                parent_conversation_id,
                conversation_history_copy_message_count,
            )
            return None
        if parent.conversation_id != parent_conversation_id:
            raise PermissionError(
                "父 Agent 会话归属校验失败，不能为子 Agent 复制历史: "
                f"parent_instance_id={parent.instance_id} "
                f"runtime_conversation_id={parent.conversation_id} "
                f"context_conversation_id={parent_conversation_id}"
            )
        if conversation_history_copy_message_count < 0:
            raise ValueError("conversation_history_copy_message_count 不能小于 0。")
        try:
            parent_conversation = await self._conversation_repository.get(parent_conversation_id)
        except Exception as exc:
            raise RuntimeError(
                f"读取父 Agent 会话失败，不能为子 Agent 复制历史: conversation_id={parent_conversation_id}"
            ) from exc
        parent_message_count = len(parent_conversation.messages)
        if conversation_history_copy_message_count > parent_message_count:
            raise ValueError(
                "conversation_history_copy_message_count 超出父会话消息数量，不能复制完整父会话: "
                f"copy_count={conversation_history_copy_message_count} parent_message_count={parent_message_count}"
            )
        child_conversation = create_conversation()
        child_conversation.instructions_snapshot = getattr(parent_conversation, "instructions_snapshot", None)
        copied_messages = parent_conversation.messages[:conversation_history_copy_message_count]
        for message in copied_messages:
            child_conversation.append_message(deepcopy(message))
        boundary_message = _build_parent_history_boundary_message(
            parent_agent_name=parent_agent_name,
            parent_instance_id=parent.instance_id,
        )
        child_conversation.append_message(
            boundary_message,
            [TextPartMeta()],
        )

        # ── 构造子会话配置时间线 ──────────────────────────────────
        copied_message_ids = {m.message_id for m in copied_messages}
        child_timeline: list[ConfigurationComponentEvent] = []

        # 1. 继承父会话历史配置并冻结为 historical 快照
        if getattr(parent_conversation, "timeline", None):
            for ev in parent_conversation.timeline:
                if ev.anchor == "start" or (
                    ev.anchor in {"before_message_id", "after_message_id"}
                    and ev.anchor_message_id in copied_message_ids
                ):
                    snapshot = ev.snapshot
                    if ev.status == "active" and snapshot is None:
                        if ev.component == "prompt":
                            snapshot = getattr(parent_conversation, "system_prompt", "") or ""
                        elif ev.component == "tool":
                            snapshot = getattr(parent_conversation, "tools", ()) or ()
                    child_timeline.append(
                        ConfigurationComponentEvent(
                            event_id=ev.event_id,
                            definition_id=ev.definition_id,
                            anchor=ev.anchor,
                            anchor_message_id=ev.anchor_message_id,
                            sequence=ev.sequence,
                            status="historical",
                            snapshot=snapshot if snapshot is not None else ("" if ev.component == "prompt" else ()),
                            revision=ev.revision,
                            component=ev.component,
                            supplementary_prompts=getattr(ev, "supplementary_prompts", ()),
                        )
                    )
        else:
            parent_def_id = getattr(parent, "agent_definition_id", "parent")
            parent_tools = getattr(parent_conversation, "tools", ())
            if parent_tools:
                child_timeline.append(
                    ConfigurationComponentEvent(
                        event_id=f"evt_tool_parent_{uuid4().hex[:8]}",
                        definition_id=parent_def_id,
                        anchor="start",
                        sequence=0,
                        status="historical",
                        snapshot=parent_tools,
                        component="tool",
                    )
                )
            parent_prompt = getattr(parent_conversation, "system_prompt", None)
            if parent_prompt:
                child_timeline.append(
                    ConfigurationComponentEvent(
                        event_id=f"evt_prompt_parent_{uuid4().hex[:8]}",
                        definition_id=parent_def_id,
                        anchor="start",
                        sequence=1,
                        status="historical",
                        snapshot=parent_prompt,
                        component="prompt",
                    )
                )

        # 2. 尝试补齐 parent_definition 和 child_definition（若未传入且 gateway 可用）
        if parent_definition is None and hasattr(self, "_agent_definition_gateway"):
            parent_def_id = getattr(parent, "agent_definition_id", None)
            if parent_def_id:
                try:
                    parent_definition = self._agent_definition_gateway.get_agent_definition(parent_def_id)
                except Exception:
                    parent_definition = None
        if child_definition is None and hasattr(self, "_agent_definition_gateway"):
            try:
                child_definition = self._agent_definition_gateway.get_agent_definition(child_agent_definition_id)
            except Exception:
                child_definition = None

        # 3. 统计父子工具集合，判断是否需要 active 工具事件
        parent_tool_names: set[str] = set()
        parent_tool_names.update(_extract_tool_names_from_tools(getattr(parent_conversation, "tools", ())))
        for ev in child_timeline:
            if ev.component == "tool" and ev.snapshot:
                parent_tool_names.update(_extract_tool_names_from_tools(ev.snapshot))
        parent_tool_names.update(_extract_definition_tool_names(parent_definition))

        child_tool_names = _extract_definition_tool_names(child_definition)
        need_child_tool_event = False
        if child_tool_names:
            need_child_tool_event = not child_tool_names.issubset(parent_tool_names)

        if need_child_tool_event:
            child_timeline.append(
                ConfigurationComponentEvent(
                    event_id=f"evt_tool_child_{uuid4().hex[:8]}",
                    definition_id=child_agent_definition_id,
                    anchor="after_message_id",
                    anchor_message_id=boundary_message.message_id,
                    sequence=0,
                    status="active",
                    component="tool",
                )
            )

        # 4. 追加子 Agent 活跃系统提示词事件，挂载阶段 ① 替换完成的补充提示词
        supplementary_prompt = SUBAGENT_SUPPLEMENTARY_PROMPT_TEMPLATE.replace(
            "{{父agent实例id}}",
            str(parent.instance_id),
        )
        child_timeline.append(
            ConfigurationComponentEvent(
                event_id=f"evt_prompt_child_{uuid4().hex[:8]}",
                definition_id=child_agent_definition_id,
                anchor="after_message_id",
                anchor_message_id=boundary_message.message_id,
                sequence=1,
                status="active",
                component="prompt",
                supplementary_prompts=(supplementary_prompt,),
            )
        )

        child_conversation.timeline = child_timeline

        await self._conversation_repository.create(
            child_conversation,
            task_id=parent.task_id,
            thread_id=child_thread_id,
            agent_id=child_agent_definition_id,
            agent_instance_id=child_instance_id,
        )
        logger.info(
            "已为子 Agent 创建父会话历史副本并静默追加范围边界消息与配置时间线: "
            "parent_instance_id=%s child_instance_id=%s parent_conversation_id=%s "
            "child_conversation_id=%s copied_parent_message_count=%s child_message_count=%s "
            "timeline_events_count=%s",
            parent.instance_id,
            child_instance_id,
            parent_conversation_id,
            child_conversation.id,
            conversation_history_copy_message_count,
            len(child_conversation.messages),
            len(child_timeline),
        )
        return child_conversation.id

    def _resolve_child_llm_selection(
        self,
        *,
        parent_llm_provider_id: str,
        parent_llm_model_binding_id: str,
        parent_llm_policy_group_id: str | None,
        child_agent_definition_id: str,
        requested_policy_group_id: str | None,
        default_policy_group_id: str | None,
        allowed_policy_group_ids: tuple[str, ...],
    ) -> dict[str, str | None]:
        policy_group_id = requested_policy_group_id or default_policy_group_id or parent_llm_policy_group_id
        if allowed_policy_group_ids and policy_group_id not in allowed_policy_group_ids:
            raise ValueError(
                f"子 Agent 定义不允许使用该 LLM 执行策略组: {child_agent_definition_id}/{policy_group_id}"
            )
        if policy_group_id:
            policy = self._llm_registry_gateway.resolve_execution_policy(policy_group_id)
            first = policy.candidates[0]
            return {
                "llm_provider_id": first.provider_id,
                "llm_model_binding_id": first.model_binding_id,
                "llm_policy_group_id": policy.policy_group_id,
            }
        return {
            "llm_provider_id": parent_llm_provider_id,
            "llm_model_binding_id": parent_llm_model_binding_id,
            "llm_policy_group_id": None,
        }

    def _is_direct_parent_child(self, task_id: str, first_instance_id: str, second_instance_id: str) -> bool:
        task = self._runtime_gateway.get_task(task_id)
        first_relation = task.child_relation_of(first_instance_id)
        if first_relation is not None and first_relation.parent_instance_id == second_instance_id:
            return True
        second_relation = task.child_relation_of(second_instance_id)
        return bool(second_relation is not None and second_relation.parent_instance_id == first_instance_id)

    def _can_message(self, *, task_id: str, from_instance_id: str, to_instance_id: str) -> bool:
        task = self._runtime_gateway.get_task(task_id)
        relation = task.child_relation_of(to_instance_id)
        if relation is not None and not relation.is_released and relation.parent_instance_id == from_instance_id:
            return True
        reverse_relation = task.child_relation_of(from_instance_id)
        return bool(
            reverse_relation is not None
            and not reverse_relation.is_released
            and reverse_relation.parent_instance_id == to_instance_id
        )

    async def _publish_subagent_released(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
        released_agent_instance_ids: list[str],
        task: AgentTask,
        reason: str,
    ) -> None:
        context = RuntimeCommunicationContext(
            stream_id=_default_stream_id_for_task(task.scene_id, task.task_id),
            actor_type="agent_instance",
            actor_id=parent_instance_id,
            trace_id=None,
            metadata={
                "task_id": task.task_id,
                "scene_id": task.scene_id,
                "agent_instance_id": child_instance_id,
                "parent_agent_instance_id": parent_instance_id,
            },
        )
        await self._event_publisher.publish(
            context,
            "agent.subagent.released",
            {
                "task_id": task.task_id,
                "parent_agent_instance_id": parent_instance_id,
                "child_agent_instance_id": child_instance_id,
                "released_agent_instance_ids": released_agent_instance_ids,
                "reason": reason,
            },
            source="subagent_runtime",
        )

    async def _publish_subagent_created(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
        relation: AgentParentChildRelation,
    ) -> None:
        child = self._runtime_gateway.get_agent_instance(child_instance_id)
        task = self._runtime_gateway.get_task(child.task_id)
        context = RuntimeCommunicationContext(
            stream_id=_default_stream_id_for_task(task.scene_id, task.task_id),
            actor_type="agent_instance",
            actor_id=parent_instance_id,
            trace_id=child.trace_id,
            metadata={
                "task_id": task.task_id,
                "scene_id": task.scene_id,
                "thread_id": child.thread_id,
                "agent_instance_id": child.instance_id,
                "agent_definition_id": child.agent_definition_id,
                "parent_agent_instance_id": parent_instance_id,
            },
        )
        await self._event_publisher.publish(
            context,
            "agent.subagent.created",
            {
                "task_id": task.task_id,
                "parent_agent_instance_id": parent_instance_id,
                "child_agent_instance_id": child.instance_id,
                "child_agent_definition_id": child.agent_definition_id,
                "relation_id": relation.relation_id,
                "released": relation.is_released,
                "created_at": relation.created_at.isoformat(),
                "agent_instance": {
                    "instance_id": child.instance_id,
                    "task_id": child.task_id,
                    "thread_id": child.thread_id,
                    "current_working_directory": child.current_working_directory,
                    "agent_definition_id": child.agent_definition_id,
                    "llm_policy_group_id": child.llm_policy_group_id,
                    "llm_provider_id": child.llm_provider_id,
                    "llm_model_binding_id": child.llm_model_binding_id,
                    "conversation_id": child.conversation_id,
                    "trace_id": child.trace_id,
                    "status": child.status,
                    "pending_user_message_count": len(child.pending_user_messages),
                    "pending_user_messages": [],
                    "context_usage": None,
                    "created_at": child.created_at.isoformat(),
                    "updated_at": child.updated_at.isoformat(),
                },
            },
            source="subagent_runtime",
        )


_runtime: SubagentRuntime | None = None


def configure_subagent_runtime(
    *,
    runtime_gateway: "AgentRuntimeGateway",
    agent_definition_gateway: "AgentDefinitionNorthGateway",
    llm_registry_gateway: "LlmModelRegistryGateway",
    execution_engine: "AgentExecutionEngine",
    event_publisher: "RuntimeEventPublisher",
    conversation_repository: "ConversationRepository",
) -> SubagentRuntime:
    global _runtime
    _runtime = SubagentRuntime(
        runtime_gateway=runtime_gateway,
        agent_definition_gateway=agent_definition_gateway,
        llm_registry_gateway=llm_registry_gateway,
        execution_engine=execution_engine,
        event_publisher=event_publisher,
        conversation_repository=conversation_repository,
    )
    return _runtime


def get_subagent_runtime() -> SubagentRuntime:
    if _runtime is None:
        raise RuntimeError("Subagent runtime has not been configured.")
    return _runtime


def _end_subagent_result_text(
    *,
    success_count: int,
    failed_count: int,
    result_lines: list[str],
) -> str:
    heading = f"批量结束完成：{success_count} 个成功，{failed_count} 个失败。"
    return "\n".join([heading, *result_lines])


def _end_subagent_result_line(
    child_instance_id: str,
    *,
    succeeded: bool,
    error_message: str | None = None,
) -> str:
    if succeeded:
        return f"{child_instance_id} 结束成功"
    return f"{child_instance_id} 结束失败，原因：{error_message or '未知错误'}"


def _running_subagent_error_message(child_instance_id: str, running_instance_id: str) -> str:
    message = "正在运行中，无法结束。只能结束暂停中的子 Agent"
    if running_instance_id != child_instance_id:
        return f"{message}（阻塞实例：{running_instance_id}）"
    return message


def _build_parent_history_boundary_message(
    *,
    parent_agent_name: str,
    parent_instance_id: str,
) -> UniMessage:
    text = (
        f"【@本地Agent {parent_agent_name} {parent_instance_id}】"
        f"上面的历史消息是我{parent_agent_name}的历史对话消息，"
        "作为你的任务背景认知上下文，只需要你帮我完成下面的任务。"
        "任务完成后请向父 Agent 汇报任务结果；"
        "如果任务执行过程中遇到问题无法确定或解决时，也要向父 Agent 汇报。"
    )
    return UniMessage(role=ROLE_USER, content=[TextPart(text)])


def _attachments_metadata(
    attachments: tuple[PendingUserMessageAttachment, ...],
) -> list[dict[str, object | None]]:
    return [
        {
            "kind": attachment.kind,
            "resource_id": attachment.resource_id,
            "name": attachment.name,
            "mime_type": attachment.mime_type,
            "size_in_bytes": attachment.size_in_bytes,
            "width": attachment.width,
            "height": attachment.height,
            "page_count": attachment.page_count,
        }
        for attachment in attachments
    ]


def _file_input_candidates(
    attachments: tuple[PendingUserMessageAttachment, ...],
) -> tuple[FileInputCandidate, ...]:
    return tuple(
        FileInputCandidate(
            kind=attachment.kind,
            size_in_bytes=attachment.size_in_bytes or 0,
            page_count=attachment.page_count or 0,
            name=attachment.name,
        )
        for attachment in attachments
    )


def _normalize_attachment_kind(value: Any, index: int) -> str:
    kind = str(value or "").strip().lower()
    if kind not in {INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF}:
        raise ValueError(f"attachments[{index}].kind 必须是 image 或 pdf。")
    return kind


def _normalize_attachment_url(value: Any, index: int) -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError(f"attachments[{index}].url 不能为空。")
    return url


def _normalize_attachment_mime_type(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _attachment_download_limit(kind: str) -> int:
    limits = default_file_input_limits()
    if kind == INPUT_MODALITY_PDF:
        return limits.max_single_input_file_bytes - 1
    return limits.max_total_image_bytes_per_request


def _default_stream_id_for_task(scene_id: str | None, task_id: str) -> str:
    if scene_id:
        return f"stream-scene-{scene_id}"
    return f"stream-agent-task-{task_id}"

