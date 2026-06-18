from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from agent_runtime.agent.agent_instance import PendingUserMessageAttachment
from agent_runtime.agent.file_input_policy import default_file_input_limits
from agent_runtime.file_resource.north_gateway.resource_create_contracts import RemoteUrlCreateRequest
from agent_runtime.file_resource.shared_service import get_file_resource_service
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.models import (
    LacpClientStatus,
    LacpInboundMessage,
    LacpInstanceRegistration,
    LacpMessageAttachment,
)
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
    from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import AgentDefinitionNorthGateway


class AgentRuntimeLacpBridge:
    def __init__(
        self,
        *,
        lacp_client: LacpClient,
        registration_registry: LacpRegistrationRegistry,
        runtime_gateway: 'AgentRuntimeGateway',
        agent_definition_gateway: 'AgentDefinitionNorthGateway',
        execution_engine: 'AgentExecutionEngine' | None = None,
    ) -> None:
        self._lacp_client = lacp_client
        self._registration_registry = registration_registry
        self._runtime_gateway = runtime_gateway
        self._agent_definition_gateway = agent_definition_gateway
        self._execution_engine = execution_engine
        self._lacp_client.set_message_handler(self.handle_inbound_message)

    def set_execution_engine(self, execution_engine: 'AgentExecutionEngine') -> None:
        self._execution_engine = execution_engine

    async def enable_instance(self, instance_id: str) -> LacpInstanceRegistration:
        instance = self._runtime_gateway.get_agent_instance(instance_id)
        definition = self._agent_definition_gateway.get_agent_definition(instance.agent_definition_id)
        registration = LacpInstanceRegistration(
            instance_id=instance.instance_id,
            role_name=definition.name or definition.agent_id,
            description=definition.description or f"AgentRuntime agent definition {definition.agent_id}",
            workspace=instance.current_working_directory,
            enabled=True,
            status="registering",
        )
        return await self._lacp_client.register_instance(registration)

    async def disable_instance(self, instance_id: str) -> LacpInstanceRegistration | None:
        await self._lacp_client.unregister_instance(instance_id)
        return self._registration_registry.get_registration(instance_id)

    def list_registrations(self) -> list[LacpInstanceRegistration]:
        return self._registration_registry.list_registrations()

    def get_registration(self, instance_id: str) -> LacpInstanceRegistration | None:
        return self._registration_registry.get_registration(instance_id)

    def get_registration_view(self, instance_id: str) -> LacpInstanceRegistration:
        registration = self._registration_registry.get_registration(instance_id)
        if registration is not None:
            return registration
        instance = self._runtime_gateway.get_agent_instance(instance_id)
        definition = self._agent_definition_gateway.get_agent_definition(instance.agent_definition_id)
        return LacpInstanceRegistration(
            instance_id=instance.instance_id,
            role_name=definition.name or definition.agent_id,
            description=definition.description or f"AgentRuntime agent definition {definition.agent_id}",
            workspace=instance.current_working_directory,
            enabled=False,
            status="disabled",
        )

    def client_status(self) -> LacpClientStatus:
        return self._lacp_client.status()

    async def reload_client_from_config(self) -> None:
        await self._lacp_client.reload_from_config()

    async def handle_inbound_message(self, message: LacpInboundMessage) -> bool:
        if self._execution_engine is None:
            logger.warning("LACP inbound message ignored because execution engine is not configured.")
            return False
        if not self._registration_registry.is_registered(message.to_instance_id):
            logger.warning("LACP inbound message ignored because target instance is not registered: %s", message.to_instance_id)
            return False
        if not message.content.strip() and not message.attachments:
            logger.warning(
                "LACP inbound message ignored because content and attachments are both empty: message_id=%s",
                message.message_id,
            )
            return False
        try:
            attachments = await self._resolve_url_attachments(message.attachments)
        except Exception:
            logger.exception("LACP inbound message attachment resolution failed: message_id=%s", message.message_id)
            return False
        user_text = self._build_user_text(message)
        metadata = self._build_message_metadata(message, attachments)
        try:
            await self._execution_engine.submit_agent_instance_message(
                agent_instance_id=message.to_instance_id,
                user_text=user_text,
                attachments=attachments,
                message_metadata=metadata,
            )
            return True
        except Exception:
            logger.exception("LACP inbound message enqueue failed: message_id=%s", message.message_id)
            return False

    @staticmethod
    def _build_user_text(message: LacpInboundMessage) -> str:
        source_tag = message.source_tag or f"@{message.from_role_name} {message.from_instance_id}".strip()
        return f"【{source_tag}】{message.content}"

    async def _resolve_url_attachments(
        self,
        attachments: tuple[LacpMessageAttachment, ...],
    ) -> tuple[PendingUserMessageAttachment, ...]:
        if not attachments:
            return ()
        requests: list[RemoteUrlCreateRequest] = []
        expected_kinds: list[str] = []
        names: list[str | None] = []
        for index, attachment in enumerate(attachments):
            kind = _normalize_attachment_kind(attachment.kind, index)
            url = _normalize_attachment_url(attachment.url, index)
            mime_type = _normalize_attachment_mime_type(attachment.mime_type)
            requests.append(
                RemoteUrlCreateRequest(
                    resource_url=url,
                    resource_mime_type=mime_type,
                    max_bytes=_attachment_download_limit(kind),
                )
            )
            expected_kinds.append(kind)
            names.append(_normalize_optional_text(attachment.name))
        service = get_file_resource_service()
        created = await monitored_to_thread(
            service.create_resources_from_remote_urls,
            requests,
            operation="lacp.resolve_url_attachments.create_resources",
            caller="AgentRuntimeLacpBridge._resolve_url_attachments",
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
            operation="lacp.resolve_url_attachments.get_resources",
            caller="AgentRuntimeLacpBridge._resolve_url_attachments",
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
                raise ValueError("LACP URL 文件资源创建后无法读取资源详情。")
            metadata = resource.resource_metadata
            actual_kind = metadata.resource_category
            if actual_kind != expected_kinds[index]:
                raise ValueError(
                    f"LACP 附件类型与 URL 文件实际类型不一致: expected={expected_kinds[index]}, actual={actual_kind}"
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

    @staticmethod
    def _build_message_metadata(
        message: LacpInboundMessage,
        attachments: tuple[PendingUserMessageAttachment, ...],
    ) -> dict[str, Any]:
        return {
            "source_type": "remote_agent",
            "transport": "lacp",
            "channel": "lacp",
            "display_text": message.content,
            "text_format": "markdown",
            "attachments": _attachments_metadata(attachments),
            "remote_agent": {
                "name": message.from_role_name,
                "id": message.from_instance_id,
                "instance_id": message.from_instance_id,
                "workspace": message.from_workspace,
            },
            "lacp": {
                "message_id": message.message_id,
                "source_tag": message.source_tag,
                "timestamp": message.timestamp,
            },
        }


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
