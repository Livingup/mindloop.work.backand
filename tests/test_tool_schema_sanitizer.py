import json

from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.tool.local_tool_factory import LocalToolFactory


EXPECTED_TOOL_SCHEMA_PLATFORMS = {
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _parameters_for_platform(platform: str, schema: dict):
    if platform == API_FORMAT_ANTHROPIC_MESSAGES:
        return schema["input_schema"]
    if platform in {API_FORMAT_OPENAI_CHAT_COMPLETIONS, API_FORMAT_ZHIPU_CHAT_COMPLETIONS}:
        return schema["function"]["parameters"]
    return schema["parameters"]


def test_local_tool_schemas_strip_pydantic_title_for_all_llm_providers():
    gateway = LocalToolFactory.create()

    schemas = gateway.get_tool("fail_task").schemas

    assert set(schemas) == EXPECTED_TOOL_SCHEMA_PLATFORMS
    for platform, schema in schemas.items():
        assert "title" not in set(_walk_keys(schema)), f"{platform} schema still exposes title: {json.dumps(schema, ensure_ascii=False)}"

        parameters = _parameters_for_platform(platform, schema)
        assert parameters["type"].lower() == "object"
        assert parameters["required"] == ["reason"]
        assert set(parameters["properties"]) == {"reason"}
        assert parameters["properties"]["reason"]["type"].lower() == "string"
        assert "description" in parameters["properties"]["reason"]


def test_create_mission_schema_preserves_title_property_while_stripping_metadata_titles():
    gateway = LocalToolFactory.create()

    schemas = gateway.get_tool("create_mission").schemas

    assert set(schemas) == EXPECTED_TOOL_SCHEMA_PLATFORMS
    for platform, schema in schemas.items():
        parameters = _parameters_for_platform(platform, schema)
        assert parameters["type"].lower() == "object"
        assert "title" in parameters["required"], f"{platform} required missing title"
        assert "title" in parameters["properties"], f"{platform} properties missing title"
        title_prop = parameters["properties"]["title"]
        assert title_prop["type"].lower() == "string"
        assert "title" not in title_prop, f"{platform} title property should not expose metadata title"
