from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.models import LacpClientStatus, LacpInboundMessage, LacpInstanceRegistration
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
        user_text = self._build_user_text(message)
        metadata = self._build_message_metadata(message)
        try:
            await self._execution_engine.submit_agent_instance_message(
                agent_instance_id=message.to_instance_id,
                user_text=user_text,
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

    @staticmethod
    def _build_message_metadata(message: LacpInboundMessage) -> dict[str, Any]:
        return {
            "source_type": "remote_agent",
            "transport": "lacp",
            "channel": "lacp",
            "display_text": message.content,
            "text_format": "markdown",
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
