import unittest
from types import SimpleNamespace

from agent_runtime.agent_definition.agent_definition import AgentDefinition, SystemPromptSegment
from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition


class _LocalToolGateway:
    def __init__(self, tools):
        self._tools = tuple(tools)

    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        return self._tools


def _local_tool(tool_name: str, *, category: str = "business", always_enabled: bool = True):
    return SimpleNamespace(
        tool_name=tool_name,
        category=category,
        always_enabled=always_enabled,
    )


def _llm_tool(tool_name: str) -> LlmToolDefinition:
    return LlmToolDefinition(name=tool_name, schema={}, source="local")


class AgentExecutionPromptToolUsageNoticeTests(unittest.TestCase):
    def _engine_with_local_tools(self, *tools) -> AgentExecutionEngine:
        engine = object.__new__(AgentExecutionEngine)
        engine._local_tool_gateway = _LocalToolGateway(tools)
        return engine

    def test_system_prompt_starts_with_communication_context_and_instance_id(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("message_user", category="lifecycle"),
        )
        prompt = engine._render_system_prompt(
            AgentDefinition(
                agent_id="agent-c",
                group="default",
                system_prompt=(
                    SystemPromptSegment(text="业务 Agent 自定义系统提示词。"),
                ),
            ),
            "agent-instance-self",
            "G:\\project\\agent",
            {},
            (_llm_tool("message_user"),),
        )

        self.assertIsNotNone(prompt)
        prompt_text = prompt or ""
        self.assertTrue(prompt_text.startswith("# 沟通环境与身份"))
        self.assertIn("当前 Agent 实例 ID：`agent-instance-self`", prompt_text)
        self.assertIn("使用 `lacp` MCP 服务提供的 `lacp_send_message` 工具", prompt_text)
        self.assertIn("禁止与远程 Agent 进行无意义礼貌性往返", prompt_text)
        self.assertLess(
            prompt_text.index("# 沟通环境与身份"),
            prompt_text.index("业务 Agent 自定义系统提示词。"),
        )

    def test_tool_usage_notice_injects_builtin_tool_ban_and_available_runtime_tool_guidance(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("message_user", category="lifecycle"),
            _local_tool("pause_task", category="lifecycle"),
            _local_tool("generate_image"),
            _local_tool("web_search"),
            _local_tool("read_url_context"),
        )
        notice = engine._build_tool_usage_notice(
            AgentDefinition(agent_id="agent-a", group="default"),
            (
                _llm_tool("message_user"),
                _llm_tool("pause_task"),
                _llm_tool("generate_image"),
                _llm_tool("web_search"),
                _llm_tool("read_url_context"),
            ),
        )

        self.assertIn("# 工具调用注意事项", notice)
        self.assertIn("禁止调用模型内置网络搜索和图片生成工具", notice)
        self.assertIn("图片生成请使用generate_image工具", notice)
        self.assertIn("网络搜索请使用web_search，read_url_context工具", notice)
        self.assertIn("禁止并发调用“message_user，pause_task”这些工具", notice)

    def test_tool_usage_notice_only_mentions_tools_visible_to_current_llm_request(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("message_user", category="lifecycle"),
            _local_tool("pause_task", category="lifecycle"),
            _local_tool("generate_image"),
            _local_tool("web_search"),
            _local_tool("read_url_context"),
        )
        notice = engine._build_tool_usage_notice(
            AgentDefinition(agent_id="agent-b", group="default"),
            (_llm_tool("file_read"),),
        )

        self.assertIn("禁止调用模型内置网络搜索和图片生成工具", notice)
        self.assertNotIn("图片生成请使用generate_image工具", notice)
        self.assertNotIn("网络搜索请使用web_search，read_url_context工具", notice)
        self.assertNotIn("禁止并发调用", notice)


if __name__ == "__main__":
    unittest.main()
