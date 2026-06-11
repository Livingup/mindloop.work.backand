import asyncio
import json
from pathlib import Path

from agent_runtime.agent.execution_loop_logger import AgentExecutionLoopLogger
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import ToolUsePart
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_gateway_impl import LocalToolGatewayImpl
from agent_runtime.tool.tool_execution_models import BaseToolResult, ToolRuntimeContext


def test_execution_loop_logger_writes_one_safe_instance_file_and_retains_latest_three_frames(tmp_path: Path) -> None:
    async def run_case() -> None:
        instance_id = "agent/instance:1?x"
        logger = AgentExecutionLoopLogger(lambda: tmp_path, retained_frame_count=3, queue_max_size=2)
        logger.start()
        try:
            logger.record_instance_event(
                instance_id=instance_id,
                stage="queue.submitted",
                status="queued",
                summary="Instance message queued outside a retained loop frame.",
                details={"message_id": "msg-1"},
                expected_next="background task launch",
            )
            for loop_index in range(5):
                frame_id = logger.begin_frame(
                    task_id="task-1",
                    instance_id=instance_id,
                    loop_index=loop_index,
                    conversation_id=f"conversation-{loop_index}",
                    stream_id="stream-1",
                    details={"candidate_count": 1},
                    expected_next="llm.request.start",
                )
                logger.record_step(
                    instance_id=instance_id,
                    frame_id=frame_id,
                    stage="tool_batch.returned",
                    status="success",
                    summary="Tool execution batch returned to the engine.",
                    details={
                        "tool_use_count": 1,
                        "tool_result_count": 1,
                        "post_tool_durability_order": [
                            "pending result persisted",
                            "aggregated ROLE_TOOL message saved",
                            "pending rows cleared",
                            "loop decision selected",
                        ],
                    },
                    expected_next="tool_results.aggregate_message.saved",
                )
                logger.end_frame(
                    instance_id=instance_id,
                    frame_id=frame_id,
                    outcome="continue" if loop_index < 4 else "pause",
                    summary="Frame finished.",
                    details={"loop_index": loop_index},
                    expected_next="next llm request" if loop_index < 4 else "pause branch",
                )
            await logger.flush_all()
        finally:
            await logger.stop()

        log_path = logger.log_file_path_for_instance(instance_id)
        assert log_path == tmp_path / "agent-execution-loop" / "agent_instance_1_x.log"
        assert sorted(path.name for path in log_path.parent.glob("*.log")) == ["agent_instance_1_x.log"]

        snapshot = json.loads(log_path.read_text(encoding="utf-8"))
        assert snapshot["instance_id"] == instance_id
        assert snapshot["retained_frame_count"] == 3
        assert snapshot["actual_frame_count"] == 3
        assert [frame["loop_index"] for frame in snapshot["frames"]] == [2, 3, 4]
        assert [event["stage"] for event in snapshot["instance_events"]] == ["queue.submitted"]
        assert all(
            "queue.submitted" not in {step["stage"] for step in frame["steps"]}
            for frame in snapshot["frames"]
        )
        last_frame = snapshot["frames"][-1]
        assert last_frame["outcome"] == "pause"
        assert [step["stage"] for step in last_frame["steps"]] == [
            "frame.start",
            "tool_batch.returned",
            "frame.end",
        ]
        tool_step = last_frame["steps"][1]
        assert tool_step["expected_next"] == "tool_results.aggregate_message.saved"
        assert tool_step["details"]["tool_result_count"] == tool_step["details"]["tool_use_count"]

    asyncio.run(run_case())


def test_execution_loop_logger_bounds_instance_events_and_records_without_started_worker(tmp_path: Path) -> None:
    async def run_case() -> None:
        instance_id = "agent-instance-2"
        logger = AgentExecutionLoopLogger(lambda: tmp_path, retained_frame_count=3)

        for index in range(130):
            logger.record_instance_event(
                instance_id=instance_id,
                stage=f"event-{index}",
                status="observed",
                summary="Bounded lifecycle event.",
                details={"index": index},
            )
        frame_id = logger.begin_frame(
            task_id="task-2",
            instance_id=instance_id,
            loop_index=1,
            conversation_id="conversation-1",
            stream_id=None,
        )
        logger.end_frame(
            instance_id=instance_id,
            frame_id=frame_id,
            outcome="complete",
            summary="Completed without a started background writer.",
        )

        await logger.flush_all()

        snapshot = json.loads(logger.log_file_path_for_instance(instance_id).read_text(encoding="utf-8"))
        assert snapshot["writer"]["started"] is False
        assert snapshot["writer"]["queue_size"] is None
        assert len(snapshot["instance_events"]) == 128
        assert snapshot["instance_events"][0]["stage"] == "event-2"
        assert snapshot["instance_events"][-1]["stage"] == "event-129"
        assert snapshot["frames"][0]["outcome"] == "complete"

    asyncio.run(run_case())


class _SingleToolRepository:
    def __init__(self, tool_def: LocalToolDefinition) -> None:
        self._tool_def = tool_def

    def list_all(self):
        return [self._tool_def]

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        if tool_name != self._tool_def.tool_name:
            raise KeyError(tool_name)
        return self._tool_def


class _SchemaCache:
    def get_all_schemas(self, _tool_def):
        return {}


def _diagnostic_echo_tool(message: str, ctx: ToolRuntimeContext) -> BaseToolResult:
    return BaseToolResult(
        success=True,
        execution_status="success",
        output={"message": message, "agent_instance_id": ctx.agent_instance_id},
    )


def test_shared_execution_loop_logger_records_dispatcher_gateway_and_service_tool_path(tmp_path: Path) -> None:
    async def run_case() -> None:
        instance_id = "agent-instance-tool-stack"
        logger = AgentExecutionLoopLogger(lambda: tmp_path, retained_frame_count=3)
        logger.start()
        try:
            frame_id = logger.begin_frame(
                task_id="task-tool",
                instance_id=instance_id,
                loop_index=1,
                conversation_id="conversation-tool",
                stream_id="stream-tool",
                expected_next="tool_dispatch.routing",
            )
            tool_def = LocalToolDefinition(
                tool_name="diagnostic_echo",
                description="Echo diagnostic input.",
                fn=_diagnostic_echo_tool,
                always_enabled=True,
            )
            service = LocalToolService(
                _SingleToolRepository(tool_def),
                _SchemaCache(),
                execution_loop_logger=logger,
            )
            gateway = LocalToolGatewayImpl(service, execution_loop_logger=logger)
            dispatcher = AgentToolDispatcher(
                local_tool_gateway=gateway,
                execution_loop_logger=logger,
            )
            result = await dispatcher.dispatch_tool_call(
                agent_definition=AgentDefinition(agent_id="agent-diagnostic", group="default"),
                tool_use=ToolUsePart(
                    name="diagnostic_echo",
                    input={"message": "hello"},
                    tool_use_id="tool-call-1",
                ),
                communication_context=RuntimeCommunicationContext(
                    stream_id="stream-tool",
                    trace_id="trace-tool",
                    metadata={
                        "task_id": "task-tool",
                        "agent_instance_id": instance_id,
                        "conversation_id": "conversation-tool",
                    },
                ),
            )
            logger.end_frame(
                instance_id=instance_id,
                frame_id=frame_id,
                outcome="continue",
                summary="Tool path finished.",
            )
            await logger.flush_all()
        finally:
            await logger.stop()

        assert result.tool_result.success is True
        snapshot = json.loads(logger.log_file_path_for_instance(instance_id).read_text(encoding="utf-8"))
        stages = [step["stage"] for step in snapshot["frames"][0]["steps"]]
        assert stages == [
            "frame.start",
            "tool_dispatch.routing",
            "tool_dispatch.local.allowlist",
            "local_tool_gateway.execute",
            "local_tool_service.execute",
            "local_tool_service.invoke_callable",
            "local_tool_service.execute",
            "local_tool_gateway.execute",
            "tool_dispatch.execute",
            "frame.end",
        ]
        gateway_start = next(
            step for step in snapshot["frames"][0]["steps"]
            if step["stage"] == "local_tool_gateway.execute" and step["status"] == "started"
        )
        assert gateway_start["details"]["trace_id"] == "trace-tool"
        service_invoke = next(step for step in snapshot["frames"][0]["steps"] if step["stage"] == "local_tool_service.invoke_callable")
        assert service_invoke["details"]["injected_runtime_context"] is True
        dispatch_complete = next(
            step for step in snapshot["frames"][0]["steps"]
            if step["stage"] == "tool_dispatch.execute" and step["status"] == "completed"
        )
        assert dispatch_complete["details"]["loop_decision"] == "continue"

    asyncio.run(run_case())
