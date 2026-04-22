"""各平台 Schema 生成器单元测试（OpenAI / Claude / Gemini）。

验证各生成器正确使用对应平台 SDK 生成工具 Schema。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_runtime.tool.infrastructure.claude_schema_generator import (
    ClaudeSchemaGenerator,
)
from agent_runtime.tool.infrastructure.gemini_schema_generator import (
    GeminiSchemaGenerator,
)
from agent_runtime.tool.infrastructure.openai_schema_generator import (
    OpenAISchemaGenerator,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


# ── 测试用工具函数 ──────────────────────────────────────────────────


def _search(query: str, max_results: int = 10) -> str:
    """搜索文件。"""
    return f"results for {query}"


class AnalyzeParams(BaseModel):
    """分析参数。"""
    text: str = Field(description="待分析文本")
    language: str = Field(default="zh", description="语言代码")


def _analyze(params: AnalyzeParams) -> str:
    return "analyzed"


def _make_tool(name: str, fn: object) -> LocalToolDefinition:
    return LocalToolDefinition(tool_name=name, description="测试工具", fn=fn)


# ── OpenAI ───────────────────────────────────────────────────────────


class TestOpenAISchemaGenerator:
    """OpenAI Schema 生成器测试（使用 openai SDK pydantic_function_tool）。"""

    def setup_method(self) -> None:
        self.gen = OpenAISchemaGenerator()

    def test_platform(self) -> None:
        assert self.gen.platform == "openai"

    def test_basic_function_format(self) -> None:
        """基本函数生成 OpenAI function calling 格式。"""
        schema = self.gen.generate(_make_tool("search", _search))
        # OpenAI 格式必须有 type: function 和 function 字段。
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "search"
        assert func["description"] == "测试工具"

    def test_basic_function_parameters(self) -> None:
        """参数 Schema 由 openai SDK 生成，包含 properties。"""
        schema = self.gen.generate(_make_tool("search", _search))
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert "query" in params["properties"]

    def test_pydantic_param(self) -> None:
        """Pydantic 模型参数直接由 openai SDK 解析。"""
        schema = self.gen.generate(_make_tool("analyze", _analyze))
        params = schema["function"]["parameters"]
        # 单一 Pydantic 参数时，_pydantic_model_builder 直接返回该模型。
        assert "text" in params["properties"]
        assert params["properties"]["text"]["description"] == "待分析文本"


# ── Claude ───────────────────────────────────────────────────────────


class TestClaudeSchemaGenerator:
    """Claude Schema 生成器测试（使用 Pydantic model_json_schema）。"""

    def setup_method(self) -> None:
        self.gen = ClaudeSchemaGenerator()

    def test_platform(self) -> None:
        assert self.gen.platform == "claude"

    def test_basic_function_format(self) -> None:
        """基本函数生成 Anthropic ToolParam 格式。"""
        schema = self.gen.generate(_make_tool("search", _search))
        assert schema["name"] == "search"
        assert schema["description"] == "测试工具"
        assert "input_schema" in schema
        # Claude 格式顶层无 type: function 字段。
        assert "type" not in schema

    def test_input_schema_structure(self) -> None:
        """input_schema 是标准 JSON Schema（由 Pydantic 生成）。"""
        schema = self.gen.generate(_make_tool("search", _search))
        input_schema = schema["input_schema"]
        assert input_schema["type"] == "object"
        assert "query" in input_schema["properties"]

    def test_pydantic_param(self) -> None:
        """Pydantic 模型参数正确生成 input_schema。"""
        schema = self.gen.generate(_make_tool("analyze", _analyze))
        input_schema = schema["input_schema"]
        assert "text" in input_schema["properties"]
        assert input_schema["properties"]["text"]["description"] == "待分析文本"


# ── Gemini ───────────────────────────────────────────────────────────


class TestGeminiSchemaGenerator:
    """Gemini Schema 生成器测试（使用 google-genai SDK types）。"""

    def setup_method(self) -> None:
        self.gen = GeminiSchemaGenerator()

    def test_platform(self) -> None:
        assert self.gen.platform == "gemini"

    def test_basic_function_format(self) -> None:
        """基本函数生成 Gemini FunctionDeclaration 格式。"""
        schema = self.gen.generate(_make_tool("search", _search))
        assert schema["name"] == "search"
        assert schema["description"] == "测试工具"
        assert "parameters" in schema
        # Gemini 格式顶层无 type: function 字段。
        assert "type" not in schema

    def test_parameters_structure(self) -> None:
        """parameters 包含 Gemini SDK 处理后的 Schema 结构。"""
        schema = self.gen.generate(_make_tool("search", _search))
        params = schema["parameters"]
        # Gemini SDK 的 Schema 使用大写枚举值（如 "OBJECT"、"STRING"）。
        assert "properties" in params

    def test_pydantic_param(self) -> None:
        """Pydantic 模型参数正确传递给 Gemini SDK。"""
        schema = self.gen.generate(_make_tool("analyze", _analyze))
        params = schema["parameters"]
        assert "text" in params["properties"]


# ── 共通验证 ─────────────────────────────────────────────────────────


def test_all_platforms_have_required_fields() -> None:
    """三个平台都正确包含工具名称和描述。"""
    tool_def = _make_tool("search", _search)
    for gen in [OpenAISchemaGenerator(), ClaudeSchemaGenerator(), GeminiSchemaGenerator()]:
        schema = gen.generate(tool_def)
        if gen.platform == "openai":
            assert schema["function"]["name"] == "search"
            assert schema["function"]["description"] == "测试工具"
        else:
            assert schema["name"] == "search"
            assert schema["description"] == "测试工具"
