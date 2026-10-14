import asyncio
import hashlib

from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.conversation_message.message.models import Conversation, ImagePart, ROLE_TOOL, TextPart, ToolUsePart, UniMessage
from agent_runtime.conversation_message.message.openai_protocol import OpenAIAdapter
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResolvedResource, ResourceMeta
from agent_runtime.file_tools.file_tools import (
    FileReadFileRequest,
    FileReadLineSliceRequest,
    FileReadRange,
    FileReadRequest,
    MAX_FILE_READ_MAX_CHARS_PER_LINE,
    file_read,
    file_read_line_slice,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_contracts import ToolExecuteRequest
from agent_runtime.tool.north_gateway.local_tool_gateway_impl import LocalToolGatewayImpl
from agent_runtime.tool.tool_execution_models import BaseToolResult


class _Repository:
    def __init__(self, tool_def: LocalToolDefinition) -> None:
        self._tool_def = tool_def

    def list_all(self):
        return [self._tool_def]

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        if tool_name != self._tool_def.tool_name:
            raise KeyError(tool_name)
        return self._tool_def


class _SchemaCache:
    def get_all_schemas(self, tool_def):
        return {}


class _AgentDefinition:
    agent_id = "agent-1"
    tool_ids = ("generate_image",)


class _EventPublisher:
    async def publish(self, *args, **kwargs):
        return None


class _Resolver:
    async def get_base64(self, resource_id: str) -> str:
        return "base64-image"

    async def get_public_url(self, resource_id: str) -> str | None:
        return "https://example.test/image.png"

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        return ResourceMeta(mime_type="image/png", resource_category="image")

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        return ResolvedResource(
            resource_id=resource_id,
            mime_type="image/png",
            resource_category="image",
            object_storage_url="https://example.test/image.png",
            base64_content=None,
            local_storage_path=None,
        )


def _generate_image_tool() -> BaseToolResult:
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=[
            TextPart(text="Image generation prompt: draw a cat"),
            ImagePart(resource_id="resource-image-1"),
        ],
    )


def test_local_tool_gateway_preserves_multimodal_tool_output_parts():
    async def run_case():
        gateway = LocalToolGatewayImpl(LocalToolService(
            _Repository(LocalToolDefinition(
                tool_name="generate_image",
                description="Generate image.",
                fn=_generate_image_tool,
            )),
            _SchemaCache(),
        ))

        result = await gateway.execute_tool(ToolExecuteRequest(tool_name="generate_image"))

        assert result.output == [
            TextPart(text="Image generation prompt: draw a cat"),
            ImagePart(resource_id="resource-image-1"),
        ]

    asyncio.run(run_case())


def test_local_multimodal_tool_output_renders_to_openai_responses_function_call_output():
    async def run_case():
        gateway = LocalToolGatewayImpl(LocalToolService(
            _Repository(LocalToolDefinition(
                tool_name="generate_image",
                description="Generate image.",
                fn=_generate_image_tool,
            )),
            _SchemaCache(),
        ))
        dispatcher = AgentToolDispatcher(
            local_tool_gateway=gateway,
            event_publisher=_EventPublisher(),
        )
        tool_use = ToolUsePart(name="generate_image", input={}, tool_use_id="call_1")

        dispatch_result = await dispatcher.dispatch_tool_call(
            agent_definition=_AgentDefinition(),
            tool_use=tool_use,
        )
        conv = Conversation(
            id="conv-local-multimodal-tool-output",
            messages=[
                UniMessage(role=ROLE_TOOL, content=[dispatch_result.result_part]),
            ],
        )

        rendered = await OpenAIAdapter().render(conv, _Resolver())

        assert rendered.input == [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "Image generation prompt: draw a cat"},
                    {"type": "input_image", "image_url": "https://example.test/image.png"},
                ],
            },
        ]

    asyncio.run(run_case())


def test_file_read_output_contains_only_llm_visible_fields(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = file_read(
        FileReadRequest(
            files=[
                FileReadFileRequest(
                    file_path=str(target),
                    ranges=[FileReadRange(start_line=1, line_count=1)],
                ),
            ],
        ),
    )

    assert result.success is True
    assert set(result.output) == {"files"}

    file_output = result.output["files"][0]
    assert file_output["file_path"] == str(target.resolve())
    assert file_output["content_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert file_output["total_lines"] == 2
    assert set(file_output) == {"file_path", "content_sha256", "total_lines", "segments"}

    segment = file_output["segments"][0]
    assert segment == {
        "start_line": 1,
        "end_line": 1,
        "lines": ["alpha"],
    }

    for unexpected_key in {
        "requested_file_count",
        "returned_file_count",
        "failed_file_count",
        "max_file_count",
        "requested_segment_count",
        "returned_segment_count",
        "file_index",
        "segment_index",
        "encoding",
        "size_bytes",
        "newline",
        "newline_counts",
        "has_final_newline",
        "warnings",
        "next_start_line",
        "truncated",
        "truncation_reason",
        "line_truncations",
    }:
        assert unexpected_key not in result.output
        assert unexpected_key not in file_output
        assert unexpected_key not in segment


def test_file_read_fails_when_requested_range_contains_large_lines(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text(
        "small\n"
        + "x" * (MAX_FILE_READ_MAX_CHARS_PER_LINE + 1)
        + "\nok\n"
        + "y" * (MAX_FILE_READ_MAX_CHARS_PER_LINE + 123)
        + "\n",
        encoding="utf-8",
    )

    result = file_read(
        FileReadRequest(
            files=[
                FileReadFileRequest(
                    file_path=str(target),
                    ranges=[FileReadRange(start_line=1, line_count=4)],
                ),
            ],
        ),
    )

    assert result.success is False
    assert result.error_code == "FileReadBatchFailed"
    file_output = result.output["files"][0]
    assert set(file_output) == {"file_path", "success", "error_code", "error_message"}
    assert file_output["file_path"] == str(target.resolve())
    assert file_output["success"] is False
    assert file_output["error_code"] == "LineTooLong"

    error_message = file_output["error_message"]
    assert "文件读取失败，部分行内容过大" in error_message
    assert "file_read_line_slice" in error_message
    assert f"第 2 行 {MAX_FILE_READ_MAX_CHARS_PER_LINE + 1} 字符" in error_message
    assert f"第 4 行 {MAX_FILE_READ_MAX_CHARS_PER_LINE + 123} 字符" in error_message
    assert "max_chars_per_line" not in error_message
    assert "large_lines" not in file_output
    assert "read_request" not in file_output


def test_file_read_line_slice_output_excludes_encoding_diagnostics(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("abcdef", encoding="utf-8")

    result = file_read_line_slice(
        FileReadLineSliceRequest(
            file_path=str(target),
            line_number=1,
            char_start=2,
        ),
    )

    assert result.success is True
    assert result.output["text"] == "bcdef"
    assert "encoding" not in result.output
