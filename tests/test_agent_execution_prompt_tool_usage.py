import asyncio
import unittest
from types import SimpleNamespace

from agent_runtime.agent_definition.agent_definition import AgentDefinition, SystemPromptSegment
from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.subagent.subagent_runtime import SubagentRuntime


class _LocalToolGateway:
    def __init__(self, tools):
        self._tools = tuple(tools)

    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        return self._tools


class _AgentDefinitionGateway:
    def __init__(self, definitions):
        self._definitions = {definition.agent_id: definition for definition in definitions}

    def get_agent_definition(self, agent_id: str) -> AgentDefinition:
        return self._definitions[agent_id]


class _SubagentTask:
    task_id = "task-1"
    is_terminal = False

    def child_relation_of(self, instance_id: str):
        if instance_id == "agent-instance-child":
            return SimpleNamespace(
                parent_instance_id="agent-instance-parent",
                is_released=False,
                release_reason=None,
            )
        return None


class _SubagentRuntimeGateway:
    def __init__(self):
        self.instances = {
            "agent-instance-parent": SimpleNamespace(
                instance_id="agent-instance-parent",
                task_id="task-1",
                agent_definition_id="parent-agent",
            ),
            "agent-instance-child": SimpleNamespace(
                instance_id="agent-instance-child",
                task_id="task-1",
                agent_definition_id="child-agent",
            ),
        }
        self.task = _SubagentTask()

    def get_agent_instance(self, instance_id: str):
        return self.instances[instance_id]

    def get_task(self, task_id: str):
        return self.task


class _ExecutionEngineSubmissionRecorder:
    def __init__(self):
        self.calls = []

    async def submit_agent_instance_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="accepted",
            task_id="task-1",
            instance_id=kwargs["agent_instance_id"],
            pending_message_id="pending-1",
            queued_for_later=False,
        )


def _local_tool(tool_name: str, *, category: str = "business", always_enabled: bool = True):
    return SimpleNamespace(
        tool_name=tool_name,
        category=category,
        always_enabled=always_enabled,
    )


def _llm_tool(tool_name: str) -> LlmToolDefinition:
    return LlmToolDefinition(name=tool_name, schema={}, source="local")


class AgentExecutionPromptToolUsageNoticeTests(unittest.TestCase):
    def _engine_with_local_tools(self, *tools, agent_definitions=()) -> AgentExecutionEngine:
        engine = object.__new__(AgentExecutionEngine)
        engine._local_tool_gateway = _LocalToolGateway(tools)
        engine._agent_definition_gateway = _AgentDefinitionGateway(agent_definitions)
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
        self.assertIn("`【@本地Agent <agent_name_or_id> <agent_instance_id>】`", prompt_text)
        self.assertIn("使用本地 subagent 工具", prompt_text)
        self.assertIn("不要用 `lacp_send_message` 联系本地子 Agent", prompt_text)
        self.assertIn("禁止与远程 Agent 进行无意义礼貌性往返", prompt_text)
        self.assertLess(
            prompt_text.index("# 沟通环境与身份"),
            prompt_text.index("业务 Agent 自定义系统提示词。"),
        )

    def test_local_subagent_message_uses_distinct_prefix_and_instance_routing(self) -> None:
        execution_engine = _ExecutionEngineSubmissionRecorder()
        runtime = SubagentRuntime(
            runtime_gateway=_SubagentRuntimeGateway(),
            agent_definition_gateway=_AgentDefinitionGateway((
                AgentDefinition(agent_id="parent-agent", group="default"),
                AgentDefinition(agent_id="child-agent", group="default", name="子助手"),
            )),
            llm_registry_gateway=SimpleNamespace(),
            execution_engine=execution_engine,
            event_publisher=SimpleNamespace(),
        )

        result = asyncio.run(runtime.send_agent_message(
            from_instance_id="agent-instance-child",
            to_instance_id="agent-instance-parent",
            content="  已完成调研。  ",
        ))

        self.assertEqual(result["target_agent_instance_id"], "agent-instance-parent")
        self.assertEqual(len(execution_engine.calls), 1)
        call = execution_engine.calls[0]
        self.assertEqual(call["agent_instance_id"], "agent-instance-parent")
        self.assertEqual(call["user_text"], "【@本地Agent 子助手 agent-instance-child】已完成调研。")
        self.assertEqual(call["message_metadata"]["source_type"], "local_agent")
        self.assertEqual(call["message_metadata"]["transport"], "subagent")
        self.assertEqual(call["message_metadata"]["channel"], "subagent")
        self.assertEqual(call["message_metadata"]["display_text"], "已完成调研。")
        self.assertEqual(call["message_metadata"]["local_agent"]["name"], "子助手")
        self.assertEqual(call["message_metadata"]["local_agent"]["instance_id"], "agent-instance-child")
        self.assertEqual(call["message_metadata"]["subagent"]["from_instance_id"], "agent-instance-child")
        self.assertEqual(call["message_metadata"]["subagent"]["to_instance_id"], "agent-instance-parent")

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

    def test_system_prompt_injects_available_subagent_ids_and_descriptions(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("create_subagent"),
            _local_tool("send_agent_message"),
            agent_definitions=(
                AgentDefinition(
                    agent_id="research-agent",
                    name="资料调研",
                    description="负责检索资料、整理事实依据，并把调研结论回传给父 Agent。",
                    system_prompt=(SystemPromptSegment(text="子 Agent 内部系统提示词不应泄漏给父 Agent。"),),
                ),
            ),
        )
        prompt = engine._render_system_prompt(
            AgentDefinition(
                agent_id="parent-agent",
                group="default",
                available_sub_agent_ids=("research-agent",),
            ),
            "agent-instance-parent",
            "G:\\project\\agent",
            {},
            (_llm_tool("create_subagent"), _llm_tool("send_agent_message")),
        )

        self.assertIsNotNone(prompt)
        prompt_text = prompt or ""
        self.assertIn("# 可用子 Agent", prompt_text)
        self.assertIn("`research-agent`", prompt_text)
        self.assertIn("负责检索资料、整理事实依据，并把调研结论回传给父 Agent。", prompt_text)
        self.assertIn("create_subagent", prompt_text)
        self.assertIn("send_agent_message", prompt_text)
        self.assertNotIn("子 Agent 内部系统提示词不应泄漏给父 Agent。", prompt_text)


if __name__ == "__main__":
    unittest.main()
