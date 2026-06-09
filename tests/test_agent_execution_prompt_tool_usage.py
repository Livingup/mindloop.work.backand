import asyncio
import unittest
from types import SimpleNamespace

from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition import AgentDefinition, McpToolBinding, SystemPromptSegment
from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpServiceSummary


class _LocalToolGateway:
    def __init__(self, tools):
        self._tools = tuple(tools)

    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        return self._tools

    def get_tool(self, tool_name: str):
        if tool_name not in {tool.tool_name for tool in self._tools}:
            raise KeyError(tool_name)
        return SimpleNamespace(
            schemas={
                API_FORMAT_OPENAI_RESPONSES: {
                    "type": "function",
                    "name": tool_name,
                    "description": f"{tool_name} meta tool.",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                },
            },
        )


class _McpGateway:
    def __init__(self):
        self.get_tool_detail_called = False

    def list_services(self):
        return [
            McpServiceSummary(
                service_id="codebase-rag",
                name="Codebase RAG",
                description="源码业务描述知识库",
                transport_type="stdio",
                enabled=True,
                connected=True,
            ),
        ]

    async def get_tool_detail(self, *_args, **_kwargs):
        self.get_tool_detail_called = True
        raise AssertionError("build_llm_tools 不应获取 MCP 工具详情来暴露直连 mcp__ 工具")


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

    def test_build_llm_tools_keeps_mcp_meta_tools_local_and_skips_direct_mcp_tool_exposure(self) -> None:
        async def run_case() -> None:
            mcp_gateway = _McpGateway()
            dispatcher = AgentToolDispatcher(
                local_tool_gateway=_LocalToolGateway((
                    _local_tool("mcp_get_tool_detail"),
                    _local_tool("mcp_call_tool"),
                )),
                mcp_client_gateway=mcp_gateway,
            )

            tools = await dispatcher.build_llm_tools(
                agent_definition=AgentDefinition(
                    agent_id="agent-mcp-tools",
                    group="default",
                    mcp_tools=(
                        McpToolBinding(
                            service_id="codebase-rag",
                            tool_ids=("codebase_search", "codebase_context_analysis"),
                        ),
                    ),
                ),
                api_format=API_FORMAT_OPENAI_RESPONSES,
            )

            tool_names = [tool.name for tool in tools]
            self.assertEqual(["mcp_get_tool_detail", "mcp_call_tool"], tool_names)
            self.assertTrue(all(tool.source == "local" for tool in tools))
            self.assertNotIn("mcp__codebase-rag__codebase_search", tool_names)
            self.assertNotIn("mcp__codebase-rag__codebase_context_analysis", tool_names)
            self.assertFalse(mcp_gateway.get_tool_detail_called)

        asyncio.run(run_case())

    def test_mcp_services_prompt_guides_meta_tools_and_records_prompt_preparation_steps(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("mcp_get_tool_detail"),
            _local_tool("mcp_call_tool"),
        )
        engine._mcp_client_gateway = _McpGateway()
        records = []

        def record_step(stage, status, summary, details, expected_next):
            records.append(
                {
                    "stage": stage,
                    "status": status,
                    "summary": summary,
                    "details": details or {},
                    "expected_next": expected_next,
                }
            )

        prompt = engine._render_system_prompt(
            AgentDefinition(
                agent_id="agent-mcp-prompt",
                group="default",
                mcp_tools=(
                    McpToolBinding(
                        service_id="codebase-rag",
                        tool_ids=("codebase_search",),
                    ),
                ),
            ),
            "agent-instance-self",
            "G:\\project\\agent",
            {},
            (_llm_tool("mcp_get_tool_detail"), _llm_tool("mcp_call_tool")),
            prompt_step_recorder=record_step,
        )

        prompt_text = prompt or ""
        self.assertIn("# 可用的 MCP 服务列表", prompt_text)
        self.assertIn("`codebase-rag`", prompt_text)
        self.assertIn("`codebase_search`", prompt_text)
        self.assertIn("mcp_get_tool_detail 获取 inputSchema", prompt_text)
        self.assertIn("mcp_call_tool 执行", prompt_text)
        self.assertNotIn("mcp__codebase-rag__codebase_search", prompt_text)

        stages = [item["stage"] for item in records]
        self.assertIn("llm.prompt.agent_segments.start", stages)
        self.assertIn("llm.prompt.runtime_context.start", stages)
        self.assertIn("llm.prompt.mcp_services.start", stages)
        self.assertIn("llm.prompt.tool_usage_notice.start", stages)
        self.assertIn("llm.prompt.assemble.complete", stages)
        mcp_complete = next(item for item in records if item["stage"] == "llm.prompt.mcp_services.complete")
        self.assertTrue(mcp_complete["details"]["mcp_services_prompt_present"])


if __name__ == "__main__":
    unittest.main()
