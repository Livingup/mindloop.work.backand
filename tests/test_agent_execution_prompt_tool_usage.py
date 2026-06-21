import asyncio
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING
from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.infrastructure.in_memory_agent_runtime_repository import InMemoryAgentRuntimeRepository
from agent_runtime.agent.infrastructure.sqlite_agent_runtime_repository import SqliteAgentRuntimeRepository
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition import AgentDefinition, McpToolBinding, SystemPromptSegment
from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_CHAT_COMPLETIONS, API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpServiceSummary
from agent_runtime.subagent.subagent_runtime import SubagentRuntime, configure_subagent_runtime
from agent_runtime.subagent.subagent_tool import EndSubagentRequest, end_subagent as end_subagent_tool
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


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
                API_FORMAT_OPENAI_CHAT_COMPLETIONS: {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"{tool_name} desc",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                API_FORMAT_OPENAI_RESPONSES: {
                    "type": "function",
                    "name": tool_name,
                    "description": f"{tool_name} meta tool.",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                },
            },
        )


class _AgentDefinitionGateway:
    def __init__(self, definitions):
        self._definitions = {definition.agent_id: definition for definition in definitions}

    def get_agent_definition(self, agent_id: str) -> AgentDefinition:
        return self._definitions[agent_id]


class _SkillGateway:
    def __init__(self, skills_dir: str = "G:\\project\\agent\\skills") -> None:
        self._skills_dir = skills_dir

    def get_settings(self):
        return SimpleNamespace(skills_dir=self._skills_dir)

    def list_skills(self):
        return []


class _McpGateway:
    def __init__(self):
        self.get_tool_detail_called = False
        self.list_tools_service_id = None

    def list_services(self):
        return [
            McpServiceSummary(
                service_id="remote-search",
                name="Remote Search",
                description="远端搜索 MCP 服务",
                transport_type="stdio",
                enabled=True,
                connected=True,
            ),
            McpServiceSummary(
                service_id="codebase-rag",
                name="Codebase RAG",
                description="源码业务描述知识库",
                transport_type="stdio",
                enabled=True,
                connected=True,
            ),
        ]

    async def list_tools(self, service_id: str):
        self.list_tools_service_id = service_id
        if service_id == "remote-search":
            return [
                SimpleNamespace(
                    name="search",
                    description="执行远端搜索",
                ),
            ]
        if service_id == "codebase-rag":
            return [
                SimpleNamespace(
                    name="codebase_search",
                    description="检索代码库业务知识",
                ),
                SimpleNamespace(
                    name="codebase_context_analysis",
                    description="分析代码库上下文",
                ),
            ]
        return []

    async def get_tool_detail(self, *_args, **_kwargs):
        self.get_tool_detail_called = True
        raise AssertionError("执行循环不应把远端 MCP 工具详情展开为 LLM tools")


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
        engine._skill_gateway = _SkillGateway()
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
        self.assertTrue(prompt_text.startswith("# 身份id和工作空间"))
        self.assertIn("你自己的 Agent 定义 ID：`agent-c`", prompt_text)
        self.assertIn("你自己的 Agent 实例 ID：`agent-instance-self`", prompt_text)
        self.assertIn("当前工作目录：`G:\\project\\agent`", prompt_text)
        self.assertIn("当前工作目录下的 Git 仓库根目录：", prompt_text)
        self.assertIn("- `G:\\project\\agent`", prompt_text)
        self.assertIn("# 系统环境", prompt_text)
        self.assertIn("- Current working directory: `G:\\project\\agent`", prompt_text)
        self.assertIn("- Operating system:", prompt_text)
        self.assertIn("- OS release:", prompt_text)
        self.assertIn("- OS version:", prompt_text)
        self.assertIn("- Machine:", prompt_text)
        self.assertIn("# 你的专属 Skill", prompt_text)
        self.assertIn("`G:\\project\\agent\\skills\\agent-c\\SKILL.md`是你的专属 Skill 文件", prompt_text)
        self.assertIn("系统会自动把他的内容追加到你系统提示词中。", prompt_text)
        self.assertIn("# 你的专属项目记忆", prompt_text)
        self.assertIn(
            "`G:\\project\\agent\\.spread-ai\\memory\\agent-c\\memory.md`是专属于你的关于当前项目的私有记忆文件",
            prompt_text,
        )
        self.assertNotIn("# 沟通环境与身份", prompt_text)
        self.assertNotIn("MCP 支持说明", prompt_text)
        self.assertNotIn("# 专属 Skill 使用协议", prompt_text)
        self.assertNotIn("# 自学习进化协议", prompt_text)
        self.assertLess(
            prompt_text.index("# 身份id和工作空间"),
            prompt_text.index("业务 Agent 自定义系统提示词。"),
        )

    def test_system_prompt_includes_pause_task_timeout_runtime_lifecycle_guidance(self) -> None:
        engine = self._engine_with_local_tools(
            _local_tool("message_user", category="lifecycle"),
            _local_tool("pause_task", category="lifecycle"),
        )
        prompt = engine._render_system_prompt(
            AgentDefinition(agent_id="agent-lifecycle", group="default"),
            "agent-instance-lifecycle",
            "G:\\project\\agent",
            {},
            (_llm_tool("message_user"), _llm_tool("pause_task")),
        )

        self.assertIsNotNone(prompt)
        prompt_text = prompt or ""
        self.assertIn("`timeout_seconds` is the pause duration in seconds", prompt_text)
        self.assertIn("omitted or `-1` means indefinite external-event-only waiting", prompt_text)
        self.assertIn("whichever arrives first — external event or timeout expiry", prompt_text)
        self.assertIn("often roughly 5–50 minutes", prompt_text)
        self.assertIn("For waiting on real user replies use `message_user(action=request_response)`, not `pause_task`", prompt_text)

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
            conversation_repository=SimpleNamespace(),
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

    def test_releasing_child_agent_deletes_runtime_instance_and_marks_relation_released(self) -> None:
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        service.create_task_with_root_agent_instance(
            task_id="task-delete-release",
            root_instance_id="agent-instance-root",
            thread_id="thread-root",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="parent-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        service.create_child_agent_instance(
            task_id="task-delete-release",
            parent_instance_id="agent-instance-root",
            child_instance_id="agent-instance-child",
            thread_id="thread-child",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="child-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )

        updated_task, released_instances = service.release_child_agent_instance(
            "agent-instance-root",
            "agent-instance-child",
            reason="parent_agent_ended_subagent",
        )

        self.assertEqual([instance.instance_id for instance in released_instances], ["agent-instance-child"])
        relation = updated_task.child_relation_of("agent-instance-child")
        self.assertIsNotNone(relation)
        self.assertTrue(relation.is_released)
        self.assertEqual(relation.release_reason, "parent_agent_ended_subagent")
        self.assertEqual(service.list_unreleased_child_agent_instances("agent-instance-root"), [])
        with self.assertRaises(KeyError):
            service.get_agent_instance("agent-instance-child")

    def test_sqlite_releasing_child_agent_deletes_instance_without_breaking_task_reload(self) -> None:
        with TemporaryDirectory() as data_dir:
            repository = SqliteAgentRuntimeRepository(data_dir)
            try:
                service = AgentRuntimeService(repository)
                service.create_task_with_root_agent_instance(
                    task_id="task-sqlite-delete-release",
                    root_instance_id="agent-instance-root",
                    thread_id="thread-root",
                    current_working_directory="G:\\project\\agent",
                    agent_definition_id="parent-agent",
                    llm_provider_id="provider",
                    llm_model_binding_id="model",
                )
                service.create_child_agent_instance(
                    task_id="task-sqlite-delete-release",
                    parent_instance_id="agent-instance-root",
                    child_instance_id="agent-instance-child",
                    thread_id="thread-child",
                    current_working_directory="G:\\project\\agent",
                    agent_definition_id="child-agent",
                    llm_provider_id="provider",
                    llm_model_binding_id="model",
                )

                updated_task, released_instances = service.release_child_agent_instance(
                    "agent-instance-root",
                    "agent-instance-child",
                    reason="parent_agent_ended_subagent",
                )

                self.assertEqual([instance.instance_id for instance in released_instances], ["agent-instance-child"])
                self.assertTrue(updated_task.child_relation_of("agent-instance-child").is_released)
                reloaded_task = service.get_task("task-sqlite-delete-release")
                self.assertEqual(reloaded_task.agent_instance_ids, ("agent-instance-root",))
                self.assertIsNone(reloaded_task.child_relation_of("agent-instance-child"))
                self.assertEqual(service.list_unreleased_child_agent_instances("agent-instance-root"), [])
                self.assertEqual(
                    [instance.instance_id for instance in service.list_task_agent_instances("task-sqlite-delete-release")],
                    ["agent-instance-root"],
                )
                with self.assertRaises(KeyError):
                    service.get_agent_instance("agent-instance-child")
            finally:
                repository.close()

    def _configure_end_subagent_test_runtime(self) -> tuple[AgentRuntimeService, SimpleNamespace]:
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        service.create_task_with_root_agent_instance(
            task_id="task-end-subagent",
            root_instance_id="agent-instance-root",
            thread_id="thread-root",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="parent-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        events = []

        async def publish(context, topic, payload, source=None):
            events.append({"context": context, "topic": topic, "payload": payload, "source": source})

        configure_subagent_runtime(
            runtime_gateway=service,
            agent_definition_gateway=_AgentDefinitionGateway(()),
            llm_registry_gateway=SimpleNamespace(),
            execution_engine=SimpleNamespace(),
            event_publisher=SimpleNamespace(publish=publish),
            conversation_repository=SimpleNamespace(),
        )
        return service, SimpleNamespace(events=events)

    def test_end_subagent_tool_succeeds_with_failed_result_for_running_child(self) -> None:
        service, recorder = self._configure_end_subagent_test_runtime()
        service.create_child_agent_instance(
            task_id="task-end-subagent",
            parent_instance_id="agent-instance-root",
            child_instance_id="agent-instance-running-child",
            thread_id="thread-running-child",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="child-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        service.start_agent_instance("agent-instance-running-child")

        tool_result = asyncio.run(end_subagent_tool(
            EndSubagentRequest(child_agent_instance_ids=["agent-instance-running-child"]),
            ToolRuntimeContext(agent_instance_id="agent-instance-root"),
        ))

        self.assertTrue(tool_result.success)
        self.assertEqual(tool_result.execution_status, "success")
        output = tool_result.output
        self.assertNotIn("status", output)
        self.assertNotIn("results", output)
        self.assertEqual(output["success_count"], 0)
        self.assertEqual(output["failed_count"], 1)
        self.assertEqual(output["released_agent_instance_ids"], [])
        self.assertEqual(
            output["result_text"],
            "批量结束完成：0 个成功，1 个失败。\n"
            "agent-instance-running-child 结束失败，原因：正在运行中，无法结束。只能结束暂停中的子 Agent",
        )
        self.assertEqual(service.get_agent_instance("agent-instance-running-child").status, AGENT_INSTANCE_STATUS_RUNNING)
        self.assertFalse(service.get_task("task-end-subagent").child_relation_of("agent-instance-running-child").is_released)
        self.assertEqual(recorder.events, [])

    def test_end_subagent_tool_releases_non_running_child(self) -> None:
        service, recorder = self._configure_end_subagent_test_runtime()
        service.create_child_agent_instance(
            task_id="task-end-subagent",
            parent_instance_id="agent-instance-root",
            child_instance_id="agent-instance-ready-child",
            thread_id="thread-ready-child",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="child-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )

        tool_result = asyncio.run(end_subagent_tool(
            EndSubagentRequest(child_agent_instance_ids=["agent-instance-ready-child"]),
            ToolRuntimeContext(agent_instance_id="agent-instance-root"),
        ))

        self.assertTrue(tool_result.success)
        output = tool_result.output
        self.assertNotIn("status", output)
        self.assertNotIn("results", output)
        self.assertEqual(output["success_count"], 1)
        self.assertEqual(output["failed_count"], 0)
        self.assertEqual(output["released_agent_instance_ids"], ["agent-instance-ready-child"])
        self.assertEqual(
            output["result_text"],
            "批量结束完成：1 个成功，0 个失败。\nagent-instance-ready-child 结束成功",
        )
        with self.assertRaises(KeyError):
            service.get_agent_instance("agent-instance-ready-child")
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]["topic"], "agent.subagent.released")

    def test_end_subagent_batch_reports_failed_running_descendant_and_releases_valid_child(self) -> None:
        service, recorder = self._configure_end_subagent_test_runtime()
        service.create_child_agent_instance(
            task_id="task-end-subagent",
            parent_instance_id="agent-instance-root",
            child_instance_id="agent-instance-blocked-child",
            thread_id="thread-blocked-child",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="child-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        service.create_child_agent_instance(
            task_id="task-end-subagent",
            parent_instance_id="agent-instance-blocked-child",
            child_instance_id="agent-instance-running-grandchild",
            thread_id="thread-running-grandchild",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="grandchild-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        service.create_child_agent_instance(
            task_id="task-end-subagent",
            parent_instance_id="agent-instance-root",
            child_instance_id="agent-instance-valid-child",
            thread_id="thread-valid-child",
            current_working_directory="G:\\project\\agent",
            agent_definition_id="child-agent",
            llm_provider_id="provider",
            llm_model_binding_id="model",
        )
        service.start_agent_instance("agent-instance-running-grandchild")

        tool_result = asyncio.run(end_subagent_tool(
            EndSubagentRequest(
                child_agent_instance_ids=["agent-instance-blocked-child", "agent-instance-valid-child"],
            ),
            ToolRuntimeContext(agent_instance_id="agent-instance-root"),
        ))
        self.assertTrue(tool_result.success)
        output = tool_result.output

        self.assertNotIn("status", output)
        self.assertNotIn("results", output)
        self.assertEqual(output["success_count"], 1)
        self.assertEqual(output["failed_count"], 1)
        self.assertEqual(output["released_agent_instance_ids"], ["agent-instance-valid-child"])
        self.assertEqual(
            output["result_text"],
            "批量结束完成：1 个成功，1 个失败。\n"
            "agent-instance-blocked-child 结束失败，原因：正在运行中，无法结束。只能结束暂停中的子 Agent"
            "（阻塞实例：agent-instance-running-grandchild）\n"
            "agent-instance-valid-child 结束成功",
        )
        self.assertFalse(service.get_task("task-end-subagent").child_relation_of("agent-instance-blocked-child").is_released)
        self.assertFalse(service.get_task("task-end-subagent").child_relation_of("agent-instance-running-grandchild").is_released)
        self.assertEqual(service.get_agent_instance("agent-instance-running-grandchild").status, AGENT_INSTANCE_STATUS_RUNNING)
        with self.assertRaises(KeyError):
            service.get_agent_instance("agent-instance-valid-child")
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]["payload"]["child_agent_instance_id"], "agent-instance-valid-child")

    def test_end_subagent_request_uses_only_child_instance_ids_list(self) -> None:
        fields = EndSubagentRequest.model_fields
        self.assertIn("child_agent_instance_ids", fields)
        self.assertNotIn("child_agent_instance_id", fields)

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

    def test_mcp_bindings_are_described_in_prompt_but_not_exposed_as_direct_llm_tools(self) -> None:
        mcp_gateway = _McpGateway()
        dispatcher = AgentToolDispatcher(
            local_tool_gateway=_LocalToolGateway((
                _local_tool("mcp_get_service_detail"),
                _local_tool("mcp_get_tool_detail"),
                _local_tool("mcp_call_tool"),
            )),
            mcp_client_gateway=mcp_gateway,
        )
        definition = AgentDefinition(
            agent_id="mcp-agent",
            group="default",
            mcp_tools=(McpToolBinding(service_id="remote-search", tool_ids=("search",)),),
        )

        llm_tools = asyncio.run(dispatcher.build_llm_tools(
            agent_definition=definition,
            api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
        ))

        self.assertEqual([tool.name for tool in llm_tools], [
            "mcp_get_service_detail",
            "mcp_get_tool_detail",
            "mcp_call_tool",
        ])
        self.assertTrue(all(tool.source == "local" for tool in llm_tools))
        self.assertFalse(any(tool.name.startswith("mcp__") for tool in llm_tools))

        engine = self._engine_with_local_tools(
            _local_tool("mcp_get_service_detail"),
            _local_tool("mcp_get_tool_detail"),
            _local_tool("mcp_call_tool"),
        )
        engine._mcp_client_gateway = mcp_gateway
        prompt = asyncio.run(engine._load_mcp_services_prompt(definition))

        self.assertIn("# 可用的 MCP 服务列表", prompt)
        self.assertIn("`remote-search`：远端搜索 MCP 服务", prompt)
        self.assertIn("`search`：执行远端搜索", prompt)
        self.assertIn("远端 MCP 服务工具不会直接设置到本轮 LLM 请求 tools 中", prompt)
        self.assertIn("`mcp_get_tool_detail` 获取参数 schema", prompt)
        self.assertIn("`mcp_call_tool` 执行", prompt)
        self.assertFalse(mcp_gateway.get_tool_detail_called)

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
        self.assertIn("`mcp_get_tool_detail` 获取参数 schema", prompt_text)
        self.assertIn("`mcp_call_tool` 执行", prompt_text)
        self.assertNotIn("mcp__codebase-rag__codebase_search", prompt_text)

        stages = [item["stage"] for item in records]
        self.assertIn("llm.prompt.agent_segments.start", stages)
        self.assertIn("llm.prompt.runtime_context.start", stages)
        self.assertIn("llm.prompt.mcp_services.start", stages)
        self.assertIn("llm.prompt.tool_usage_notice.start", stages)
        self.assertIn("llm.prompt.assemble.complete", stages)
        mcp_complete = next(item for item in records if item["stage"] == "llm.prompt.mcp_services.complete")
        self.assertTrue(mcp_complete["details"]["mcp_services_prompt_present"])

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
        self.assertIn("- agent定义id：`research-agent`；功能：负责检索资料、整理事实依据，并把调研结论回传给父 Agent。", prompt_text)
        self.assertIn("create_subagent", prompt_text)
        self.assertIn("send_agent_message", prompt_text)
        self.assertNotIn("子 Agent 内部系统提示词不应泄漏给父 Agent。", prompt_text)


if __name__ == "__main__":
    unittest.main()
