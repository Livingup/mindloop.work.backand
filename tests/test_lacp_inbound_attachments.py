import asyncio
from types import SimpleNamespace

from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge
from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.config import LacpConfig
from agent_runtime.lacp.models import LacpInboundMessage
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry


class _LacpClientStub:
    def __init__(self) -> None:
        self.handler = None

    def set_message_handler(self, handler) -> None:
        self.handler = handler


class _RegistrationRegistryStub:
    def __init__(self, registered_instance_id: str) -> None:
        self.registered_instance_id = registered_instance_id

    def is_registered(self, instance_id: str) -> bool:
        return instance_id == self.registered_instance_id

    def get_registration(self, instance_id: str):
        return None

    def list_registrations(self) -> list:
        return []


class _ExecutionEngineStub:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def submit_agent_instance_message(self, **kwargs):
        if self.fail:
            raise ValueError("file input rejected")
        self.calls.append(kwargs)
        return SimpleNamespace(status="queued")


class _FileResourceServiceStub:
    def __init__(self) -> None:
        self.create_requests = []

    def create_resources_from_remote_urls(self, requests):
        self.create_requests = list(requests)
        return [
            SimpleNamespace(resource_id=f"resource-{index}")
            for index, _request in enumerate(requests, start=1)
        ]

    def get_resources(self, resource_ids):
        resources = []
        for index, resource_id in enumerate(resource_ids):
            if index == 0:
                metadata = SimpleNamespace(
                    resource_category=INPUT_MODALITY_IMAGE,
                    image_width=640,
                    image_height=480,
                )
                resources.append(
                    SimpleNamespace(
                        resource_id=resource_id,
                        resource_mime_type="image/png",
                        resource_size_in_bytes=1234,
                        resource_metadata=metadata,
                    )
                )
            else:
                metadata = SimpleNamespace(
                    resource_category=INPUT_MODALITY_PDF,
                    pdf_page_count=3,
                )
                resources.append(
                    SimpleNamespace(
                        resource_id=resource_id,
                        resource_mime_type="application/pdf",
                        resource_size_in_bytes=4096,
                        resource_metadata=metadata,
                    )
                )
        return resources


def test_lacp_bridge_resolves_structured_attachments_before_enqueue(monkeypatch) -> None:
    file_service = _FileResourceServiceStub()
    monkeypatch.setattr("agent_runtime.lacp.bridge.get_file_resource_service", lambda: file_service)
    engine = _ExecutionEngineStub()
    bridge = AgentRuntimeLacpBridge(
        lacp_client=_LacpClientStub(),
        registration_registry=_RegistrationRegistryStub("agent-target"),
        runtime_gateway=object(),
        agent_definition_gateway=object(),
        execution_engine=engine,
    )
    message = LacpInboundMessage.from_frame(
        {
            "type": "message",
            "message_id": "message-1",
            "source_tag": "@Remote agent-source",
            "from_instance_id": "agent-source",
            "from_role_name": "Remote",
            "from_workspace": "D:/project/lacp",
            "to_instance_id": "agent-target",
            "content": "- 请查看附件",
            "attachments": [
                {"kind": "image", "url": "https://example.test/image.png", "name": "diagram.png", "mime_type": "image/png"},
                {"kind": "pdf", "url": "https://example.test/doc.pdf", "name": "spec.pdf", "mime_type": "application/pdf"},
            ],
        }
    )

    received = asyncio.run(bridge.handle_inbound_message(message))

    assert received is True
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call["agent_instance_id"] == "agent-target"
    assert call["user_text"] == "【@Remote agent-source】- 请查看附件"
    assert [attachment.kind for attachment in call["attachments"]] == [INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF]
    assert [attachment.resource_id for attachment in call["attachments"]] == ["resource-1", "resource-2"]
    assert call["attachments"][0].width == 640
    assert call["attachments"][1].page_count == 3
    assert call["message_metadata"]["source_type"] == "remote_agent"
    assert call["message_metadata"]["transport"] == "lacp"
    assert call["message_metadata"]["text_format"] == "markdown"
    assert call["message_metadata"]["attachments"][0]["resource_id"] == "resource-1"
    assert call["message_metadata"]["attachments"][1]["name"] == "spec.pdf"
    assert file_service.create_requests[0].max_bytes == 512 * 1024 * 1024
    assert file_service.create_requests[1].max_bytes == 50 * 1024 * 1024 - 1


def test_lacp_bridge_returns_false_when_enqueue_validation_fails(monkeypatch) -> None:
    file_service = _FileResourceServiceStub()
    monkeypatch.setattr("agent_runtime.lacp.bridge.get_file_resource_service", lambda: file_service)
    bridge = AgentRuntimeLacpBridge(
        lacp_client=_LacpClientStub(),
        registration_registry=_RegistrationRegistryStub("agent-target"),
        runtime_gateway=object(),
        agent_definition_gateway=object(),
        execution_engine=_ExecutionEngineStub(fail=True),
    )
    message = LacpInboundMessage.from_frame(
        {
            "message_id": "message-1",
            "source_tag": "@Remote agent-source",
            "from_instance_id": "agent-source",
            "from_role_name": "Remote",
            "to_instance_id": "agent-target",
            "content": "",
            "attachments": [
                {"kind": "image", "url": "https://example.test/image.png"},
            ],
        }
    )

    assert asyncio.run(bridge.handle_inbound_message(message)) is False


def test_lacp_client_ack_uses_handler_result_after_message_processing(monkeypatch) -> None:
    registry = LacpRegistrationRegistry(lambda: "")
    client = LacpClient(
        config_accessor=lambda: LacpConfig(enabled=True, registry_url="ws://example.test/ws"),
        registration_registry=registry,
    )
    sent_frames = []

    async def handler(message: LacpInboundMessage) -> bool:
        assert message.attachments[0].url == "https://example.test/image.png"
        return False

    async def send_json(frame):
        sent_frames.append(frame)

    client.set_message_handler(handler)
    monkeypatch.setattr(client, "_send_json", send_json)

    asyncio.run(
        client._handle_message_frame(
            {
                "type": "message",
                "message_id": "message-1",
                "from_instance_id": "agent-source",
                "from_role_name": "Remote",
                "to_instance_id": "agent-target",
                "content": "",
                "attachments": [{"kind": "image", "url": "https://example.test/image.png"}],
            }
        )
    )

    assert sent_frames == [{"type": "ack", "message_id": "message-1", "received": False}]
