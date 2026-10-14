import asyncio
from types import SimpleNamespace

from agent_runtime.agent.agent_instance import PendingUserMessageAttachment
from agent_runtime.agent.file_input_policy import collect_next_request_file_input_stats
from agent_runtime.conversation_message.message.models import ImagePart
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE


class _ConversationRepositoryStub:
    def __init__(self, messages):
        self.messages = messages

    async def get(self, conversation_id):
        return SimpleNamespace(conversation_id=conversation_id, messages=self.messages)


class _BatchLimitedFileResourceServiceStub:
    def __init__(self, *, batch_limit=20):
        self._batch_limit = batch_limit
        self.calls = []

    def get_resources(self, resource_ids):
        self.calls.append(list(resource_ids))
        if len(resource_ids) > self._batch_limit:
            raise ValueError(f"batch too large: {len(resource_ids)} > {self._batch_limit}")
        return [
            SimpleNamespace(
                resource_id=resource_id,
                resource_size_in_bytes=100,
                resource_metadata=SimpleNamespace(resource_category=INPUT_MODALITY_IMAGE),
            )
            for resource_id in resource_ids
        ]


def test_collect_next_request_file_input_stats_batches_history_resources() -> None:
    messages = [
        SimpleNamespace(
            content=[ImagePart(resource_id=f"resource-{index}") for index in range(21)]
        )
    ]
    file_service = _BatchLimitedFileResourceServiceStub(batch_limit=20)
    instance = SimpleNamespace(conversation_id="conversation-1", pending_user_messages=())

    stats = asyncio.run(
        collect_next_request_file_input_stats(
            instance,
            _ConversationRepositoryStub(messages),
            file_service,
        )
    )

    assert stats.image_file_count == 21
    assert stats.total_image_bytes == 2100
    assert [len(call) for call in file_service.calls] == [20, 1]


def test_collect_next_request_file_input_stats_batches_pending_attachments() -> None:
    attachments = tuple(
        PendingUserMessageAttachment(
            kind=INPUT_MODALITY_IMAGE,
            resource_id=f"resource-{index}",
            name=f"image-{index}.png",
        )
        for index in range(21)
    )
    file_service = _BatchLimitedFileResourceServiceStub(batch_limit=20)
    instance = SimpleNamespace(
        conversation_id=None,
        pending_user_messages=(SimpleNamespace(attachments=attachments),),
    )

    stats = asyncio.run(
        collect_next_request_file_input_stats(
            instance,
            None,
            file_service,
        )
    )

    assert stats.image_file_count == 21
    assert stats.total_image_bytes == 2100
    assert [len(call) for call in file_service.calls] == [20, 1]
