import asyncio
import logging
import sys
from dataclasses import replace
from contextlib import suppress
from types import SimpleNamespace

from agent_runtime.agent import agent_execution_engine as agent_execution_engine_module
from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine, AgentExecutionRequest, AgentExecutionResult
from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    PendingAgentUserMessage,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.north_gateway import agent_runtime_gateway_impl as agent_runtime_gateway_impl_module
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry, runtime_task_registry_admission_policy
from agent_runtime.llm_client import llm_request_service as llm_request_service_module
from agent_runtime.lacp import client as lacp_client_module
from agent_runtime.lacp.client import LacpClient, _safe_ws_owner_id
from agent_runtime.lacp.config import LacpConfig
from agent_runtime.mcp.mcp_client.infrastructure import mcp_session_manager as mcp_session_manager_module
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import McpSessionManager
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_transport_config import StdioTransportConfig
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    MCP_CONNECTION_STATUS_DISCONNECTED,
    McpServiceCapabilities,
)
from agent_runtime.runtime_health import (
    runtime_concurrency_health_snapshot,
    runtime_concurrency_limit,
    runtime_task_health_summary,
)


def test_runtime_task_registry_tracks_cancel_stop_health_and_redaction() -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        task = registry.create_task(
            wait_forever(),
            task_name="test-background-worker",
            owner_type="test_owner",
            owner_id="owner-1",
            task_class="worker",
            diagnostic_context={
                "authorization": "Bearer secret-token",
                "command_line": "python secret.py --token hidden",
                "source_url": "https://user:password@example.com:8443/path?q=secret",
                "safe_label": "visible",
            },
        )
        task_id = registry.task_id_for(task)
        assert task_id is not None

        registry.mark_progress(task, diagnostic_context={"payload": {"secret": "hidden"}, "safe_counter": 1})
        snapshot = registry.snapshot(include_completed=False)
        assert snapshot["active_count"] == 1
        record = snapshot["tasks"][0]
        assert record["task_id"] == task_id
        assert record["task_name"] == "test-background-worker"
        assert record["owner_type"] == "test_owner"
        assert record["owner_id"] == "owner-1"
        assert record["task_class"] == "worker"
        assert record["status"] == "running"
        assert record["last_progress_at"] is not None
        assert record["health_snapshot"]["running"] is True
        assert record["health_snapshot"]["terminal"] is False
        assert record["diagnostic_context"]["authorization"] == "[redacted]"
        assert record["diagnostic_context"]["command_line"] == "[redacted]"
        assert record["diagnostic_context"]["source_url"] == "https://example.com:8443/path"
        assert record["diagnostic_context"]["payload"] == "[redacted]"
        assert record["diagnostic_context"]["safe_counter"] == 1

        assert registry.cancel(task, message="stop for test", diagnostic_context={"phase": "test_shutdown"}) is True
        cancelling = registry.snapshot(include_completed=False)["tasks"][0]
        assert cancelling["status"] == "cancelling"
        assert cancelling["cancel_requested"] is True
        assert cancelling["stop_requested"] is True
        assert cancelling["cancel_requested_at"] is not None
        assert cancelling["stop_requested_at"] is not None

        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

        completed = registry.snapshot()["tasks"][0]
        assert completed["status"] == "cancelled"
        assert completed["last_error_type"] == "CancelledError"
        assert completed["health_snapshot"]["terminal"] is True
        assert completed["health_snapshot"]["cancel_requested"] is True
        assert completed["health_snapshot"]["stop_requested"] is True

    asyncio.run(run_case())


def test_runtime_task_registry_register_alias_and_done_callback_failure_are_queryable(caplog) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)

        async def finish() -> str:
            return "ok"

        task = asyncio.create_task(finish(), name="pre-created-task")
        original_on_done = registry._on_task_done

        def fail_once(done_task: asyncio.Task[object]) -> None:
            registry._on_task_done = original_on_done
            raise RuntimeError("done callback secret token should be redacted")

        registry._on_task_done = fail_once
        with caplog.at_level(logging.ERROR, logger="agent_runtime.infrastructure.runtime_task_registry"):
            registered = registry.register(
                task,
                owner_type="test_owner",
                owner_id="owner-2",
                task_class="registered_worker",
                diagnostic_context={"safe": "value"},
            )
            assert registered is task
            await task
            await asyncio.sleep(0)

        failed_callback_record = registry.snapshot()["tasks"][0]
        assert failed_callback_record["task_name"] == "pre-created-task"
        assert failed_callback_record["done_callback_error_type"] == "RuntimeError"
        assert failed_callback_record["done_callback_error"] == "[redacted]"
        assert failed_callback_record["health_snapshot"]["has_error"] is True
        assert "[redacted]" in caplog.text
        assert "done callback secret token should be redacted" not in caplog.text
        assert "Traceback" not in caplog.text

        second_task = registry.create(
            finish(),
            task_name="created-via-alias",
            owner_type="test_owner",
            owner_id="owner-3",
            task_class="alias_worker",
        )
        await second_task
        await asyncio.sleep(0)

        alias_record = next(record for record in registry.snapshot()["tasks"] if record["task_name"] == "created-via-alias")
        assert alias_record["status"] == "completed"
        assert alias_record["last_error"] is None

    asyncio.run(run_case())


def test_runtime_task_registry_admission_policy_codifies_stage4_scope() -> None:
    policy = runtime_task_registry_admission_policy()

    assert "long_lived_background_worker" in policy["required_registry_categories"]
    assert "reconnect_or_subscription_owner" in policy["required_registry_categories"]
    assert "request_local_anext_subscription_driver" in policy["direct_create_task_exceptions"]
    assert any("cancellation" in item for item in policy["exception_minimum_requirements"])
    assert "mcp_session_manager" in policy["owner_naming_rules"]["owner_type"]
    assert "websocket_reader" in policy["owner_naming_rules"]["task_class"]


def test_runtime_task_health_summary_is_bounded_and_sanitized(monkeypatch) -> None:
    async def run_case() -> None:
        import agent_runtime.runtime_health as runtime_health

        registry = RuntimeTaskRegistry(completed_task_retention=8)

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        active_task = registry.create_task(
            wait_forever(),
            task_name="test-active-worker",
            owner_type="test_owner",
            owner_id="owner-1",
            task_class="worker",
            diagnostic_context={"authorization": "Bearer secret-token", "safe_label": "visible"},
        )
        cancelling_task = registry.create_task(
            wait_forever(),
            task_name="test-cancelling-worker",
            owner_type="test_owner",
            owner_id="owner-2",
            task_class="worker",
            diagnostic_context={"payload": "raw-frame"},
        )
        registry.cancel(cancelling_task, message="stop", diagnostic_context={"phase": "test_cancel"})

        monkeypatch.setattr(runtime_health, "get_runtime_task_registry", lambda: registry)
        summary = runtime_task_health_summary(stale_progress_seconds=0, stale_limit=4, cancelling_limit=4)

        registry.cancel(active_task, message="test cleanup")
        with suppress(asyncio.CancelledError):
            await active_task
        with suppress(asyncio.CancelledError):
            await cancelling_task

        assert summary["total_count"] == 2
        assert summary["active_count"] == 2
        assert summary["by_status"]["running"] == 1
        assert summary["by_status"]["cancelling"] == 1
        assert summary["active_by_owner"]["test_owner:owner-1"] == 1
        assert summary["active_by_class"]["worker"] == 2
        assert summary["stale_progress_count"] == 2
        assert summary["cancelling_count"] == 1
        cancelling_item = summary["cancelling_tasks"][0]
        assert cancelling_item["task_name"] == "test-cancelling-worker"
        assert "diagnostic_context" not in cancelling_item
        assert "last_error" not in cancelling_item
        assert "authorization" not in str(summary).lower()
        assert "raw-frame" not in str(summary)

    asyncio.run(run_case())


def test_runtime_concurrency_health_snapshot_is_bounded_sanitized_and_diagnostic() -> None:
    runtime_tasks = {
        "total_count": 4,
        "active_count": 3,
        "by_status": {"running": 2, "cancelling": 1, "failed": 1},
        "active_by_owner": {"owner:one": 5, "owner:two": 2},
        "active_by_class": {"worker": 5, "reader": 2},
        "stale_progress_seconds": 300.0,
        "stale_progress_count": 1,
        "stale_progress_tasks": [
            {
                "task_id": "task-stale",
                "task_name": "stale-worker",
                "owner_type": "test",
                "owner_id": "one",
                "task_class": "worker",
                "status": "running",
                "seconds_since_progress": 900,
            }
        ],
        "cancelling_count": 1,
        "cancelling_tasks": [
            {
                "task_id": "task-cancelling",
                "task_name": "cancelling-worker",
                "owner_type": "test",
                "owner_id": "two",
                "task_class": "worker",
                "status": "cancelling",
            }
        ],
        "recent_terminal_tasks": [
            {
                "task_id": "task-failed",
                "task_name": "failed-worker",
                "owner_type": "test",
                "owner_id": "three",
                "task_class": "worker",
                "status": "failed",
                "last_error_type": "RuntimeError",
            }
        ],
    }
    blocking_scheduler = {
        "pools": {
            "communication": {
                "submitted": 7,
                "pending": 2,
                "active": 4,
                "completed": 1,
                "failed": 0,
                "cancelled": 0,
                "operations": {
                    "publish.secret.token": {
                        "submitted": 7,
                        "pending": 2,
                        "active": 4,
                        "completed": 1,
                        "failed": 0,
                        "cancelled": 0,
                    }
                },
            },
            "db": {
                "submitted": 1,
                "pending": 0,
                "active": 0,
                "completed": 0,
                "failed": 1,
                "cancelled": 0,
                "operations": {},
            },
        },
        "executors": {
            "communication": {
                "max_workers": 4,
                "isolated": True,
                "executor_pool": "communication",
                "runtime": {
                    "work_queue_size": 2,
                    "thread_count": 4,
                    "alive_thread_count": 4,
                    "shutdown": False,
                    "broken": None,
                },
            }
        },
    }
    network = {
        "active_http_requests": 3,
        "active_sse_connections": 2,
        "event_hub_queue_full_count": 1,
        "dropped_log_records": 0,
        "queued_log_records": 5,
        "url": "https://user:password@example.test/path?token=secret",
        "authorization_header": "Bearer secret-token",
        "raw_payload": "should-not-appear",
    }

    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={
            "status": "slow_recently",
            "monitor_running": True,
            "last_lag_ms": 2500,
            "max_lag_ms": 3000,
            "slow_tick_count": 2,
            "blocked_tick_count": 0,
        },
        network_snapshot=network,
        runtime_task_summary=runtime_tasks,
        blocking_scheduler_snapshot=blocking_scheduler,
        top_limit=1,
    )

    assert set(snapshot) == {
        "status",
        "diagnostic_scope",
        "diagnostic_only",
        "signals",
        "event_loop",
        "blocking_scheduler",
        "runtime_tasks",
        "network",
        "limits",
    }
    assert snapshot["status"] == "degraded"
    assert snapshot["diagnostic_scope"] == "runtime_concurrency_observability"
    assert snapshot["diagnostic_only"] is True
    assert snapshot["limits"] == {
        "top_limit": 1,
        "operation_limit": 0,
        "include_blocking_operations": False,
    }
    assert snapshot["signals"]["event_loop"]["lagging"] is True
    assert snapshot["signals"]["runtime_tasks"]["stale_progress"] is True
    assert snapshot["signals"]["network"]["event_hub_queue_pressure"] is True
    blocking = snapshot["blocking_scheduler"]
    assert {
        "pool_count",
        "executor_count",
        "physical_executor_count",
        "operation_count",
        "totals",
        "logical_totals",
        "physical_executor_totals",
        "top_active_pools",
        "top_pending_pools",
        "top_failed_pools",
        "top_queued_executors",
        "unavailable_executors",
        "top_physical_executors",
        "unavailable_physical_executors",
        "executor_view",
    }.issubset(blocking)
    assert blocking["totals"]["pending"] == 2
    assert blocking["totals"]["executor_work_queue_size"] == 2
    assert len(blocking["top_active_pools"]) == 1
    assert len(blocking["top_pending_pools"]) == 1
    assert len(blocking["top_failed_pools"]) == 1
    assert len(blocking["top_queued_executors"]) == 1
    assert len(blocking["top_physical_executors"]) == 1
    assert len(snapshot["runtime_tasks"]["top_active_owners"]) == 1
    assert "top_operations" not in blocking
    payload_text = str(snapshot).lower()
    assert "diagnostic_context" not in payload_text
    assert "should-not-appear" not in payload_text
    assert "authorization" not in payload_text
    assert "password" not in payload_text
    assert "token" not in payload_text
    assert "header" not in payload_text
    assert "payload" not in payload_text
    assert "body" not in payload_text
    assert "content" not in payload_text
    assert "base64" not in payload_text
    assert "0x" not in payload_text



def test_runtime_concurrency_limit_keeps_public_default_and_maximum_contract() -> None:
    assert runtime_concurrency_limit(None) == 20
    assert runtime_concurrency_limit(0) == 20
    assert runtime_concurrency_limit("not-a-number") == 20
    assert runtime_concurrency_limit(7) == 7
    assert runtime_concurrency_limit(101) == 100


def test_runtime_concurrency_health_snapshot_empty_executor_view_is_explicit_and_stable() -> None:
    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={"status": "ok", "monitor_running": True},
        network_snapshot={},
        runtime_task_summary=_empty_runtime_task_summary(),
        blocking_scheduler_snapshot={
            "pools": {},
            "executors": {
                "default": {
                    "max_workers": 8,
                    "isolated": False,
                    "executor_pool": "default",
                    "created": False,
                    "runtime": {},
                }
            },
        },
    )

    blocking = snapshot["blocking_scheduler"]
    assert snapshot["status"] == "ok"
    assert snapshot["diagnostic_only"] is True
    assert snapshot["limits"] == {
        "top_limit": 20,
        "operation_limit": 0,
        "include_blocking_operations": False,
    }
    assert blocking["executor_count"] == 1
    assert blocking["physical_executor_count"] == 0
    assert blocking["physical_executor_totals"] == {
        "executor_work_queue_size": 0,
        "executor_thread_count": 0,
        "executor_alive_thread_count": 0,
        "executor_max_workers": 0,
        "executor_shutdown_count": 0,
        "executor_broken_count": 0,
    }
    assert blocking["executor_view"] == {
        "logical_executor_count": 1,
        "physical_executor_count": 0,
        "shared_executor_count": 0,
        "dedupe_identity_source": "none",
        "dedupe_confident": True,
        "physical_totals_deduped": True,
    }
    assert snapshot["signals"]["blocking_scheduler"]["physical_executor_count"] == 0
    assert snapshot["signals"]["blocking_scheduler"]["logical_executor_count"] == 1
    assert snapshot["signals"]["blocking_scheduler"]["shared_executors_detected"] is False
    assert "top_operations" not in blocking


def test_runtime_concurrency_health_snapshot_detail_is_bounded_and_keeps_operation_counters_only() -> None:
    blocking_scheduler = {
        "pools": {
            f"pool-{index}": {
                "submitted": index + 1,
                "pending": index,
                "active": index,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "operations": {
                    f"operation-{index}": {
                        "submitted": index + 1,
                        "pending": index,
                        "active": index,
                        "completed": 0,
                        "failed": 0,
                        "cancelled": 0,
                    }
                },
            }
            for index in range(5)
        },
        "executors": {},
    }

    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={"status": "ok", "monitor_running": True},
        network_snapshot={},
        runtime_task_summary={
            "total_count": 0,
            "active_count": 0,
            "by_status": {},
            "active_by_owner": {},
            "active_by_class": {},
            "stale_progress_seconds": 300.0,
            "stale_progress_count": 0,
            "stale_progress_tasks": [],
            "cancelling_count": 0,
            "cancelling_tasks": [],
            "recent_terminal_tasks": [],
        },
        blocking_scheduler_snapshot=blocking_scheduler,
        top_limit=2,
        operation_limit=2,
        include_blocking_operations=True,
    )

    assert snapshot["status"] == "attention"
    assert snapshot["limits"] == {
        "top_limit": 2,
        "operation_limit": 2,
        "include_blocking_operations": True,
    }
    assert len(snapshot["blocking_scheduler"]["top_active_pools"]) == 2
    assert len(snapshot["blocking_scheduler"]["top_operations"]) == 2
    operation = snapshot["blocking_scheduler"]["top_operations"][0]
    assert set(operation) == {
        "pool",
        "operation",
        "submitted",
        "pending",
        "active",
        "completed",
        "failed",
        "cancelled",
        "rejected",
        "overloaded",
    }



def _empty_runtime_task_summary() -> dict[str, object]:
    return {
        "total_count": 0,
        "active_count": 0,
        "by_status": {},
        "active_by_owner": {},
        "active_by_class": {},
        "stale_progress_seconds": 300.0,
        "stale_progress_count": 0,
        "stale_progress_tasks": [],
        "cancelling_count": 0,
        "cancelling_tasks": [],
        "recent_terminal_tasks": [],
    }


def test_runtime_concurrency_health_snapshot_separates_logical_and_deduped_physical_executor_totals() -> None:
    blocking_scheduler = {
        "pools": {
            "file_resource": {
                "submitted": 10,
                "pending": 2,
                "active": 5,
                "completed": 3,
                "failed": 0,
                "cancelled": 0,
                "operations": {},
            },
            "conversation": {
                "submitted": 8,
                "pending": 4,
                "active": 3,
                "completed": 1,
                "failed": 0,
                "cancelled": 0,
                "operations": {},
            },
            "communication": {
                "submitted": 2,
                "pending": 0,
                "active": 1,
                "completed": 1,
                "failed": 1,
                "cancelled": 0,
                "operations": {},
            },
        },
        "executors": {
            "file_resource": {
                "max_workers": 8,
                "isolated": False,
                "executor_pool": "default",
                "created": True,
                "runtime": {
                    "executor_id": "0xabc123",
                    "max_workers": 8,
                    "work_queue_size": 5,
                    "thread_count": 8,
                    "alive_thread_count": 7,
                    "shutdown": False,
                    "broken": None,
                },
            },
            "conversation": {
                "max_workers": 8,
                "isolated": False,
                "executor_pool": "default",
                "created": True,
                "runtime": {
                    "executor_id": "0xabc123",
                    "max_workers": 8,
                    "work_queue_size": 5,
                    "thread_count": 8,
                    "alive_thread_count": 7,
                    "shutdown": False,
                    "broken": None,
                },
            },
            "communication": {
                "max_workers": 2,
                "isolated": True,
                "executor_pool": "communication",
                "created": True,
                "runtime": {
                    "executor_id": "0xdef456",
                    "max_workers": 2,
                    "work_queue_size": 1,
                    "thread_count": 2,
                    "alive_thread_count": 2,
                    "shutdown": False,
                    "broken": None,
                },
            },
        },
    }

    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={"status": "ok", "monitor_running": True},
        network_snapshot={},
        runtime_task_summary=_empty_runtime_task_summary(),
        blocking_scheduler_snapshot=blocking_scheduler,
        top_limit=10,
    )

    blocking = snapshot["blocking_scheduler"]
    assert blocking["executor_count"] == 3
    assert blocking["physical_executor_count"] == 2
    assert blocking["logical_totals"]["active"] == 9
    assert blocking["logical_totals"]["pending"] == 6
    assert blocking["physical_executor_totals"]["executor_max_workers"] == 10
    assert blocking["physical_executor_totals"]["executor_work_queue_size"] == 6
    assert blocking["totals"]["active"] == 9
    assert blocking["totals"]["pending"] == 6
    assert blocking["totals"]["executor_max_workers"] == 10
    assert blocking["totals"]["executor_work_queue_size"] == 6
    assert snapshot["signals"]["blocking_scheduler"]["active_operations"] == 9
    assert snapshot["signals"]["blocking_scheduler"]["pending_operations"] == 6
    assert snapshot["signals"]["blocking_scheduler"]["executor_queue_size"] == 6
    assert snapshot["signals"]["blocking_scheduler"]["executor_runtime_totals_deduped"] is True
    assert snapshot["signals"]["blocking_scheduler"]["shared_executors_detected"] is True
    assert snapshot["signals"]["blocking_scheduler"]["physical_executor_count"] == 2
    assert snapshot["signals"]["blocking_scheduler"]["logical_executor_count"] == 3

    executor_view = blocking["executor_view"]
    assert executor_view["logical_executor_count"] == 3
    assert executor_view["physical_executor_count"] == 2
    assert executor_view["shared_executor_count"] == 1
    assert executor_view["physical_totals_deduped"] is True

    default_executor = next(
        item for item in blocking["top_physical_executors"] if item["physical_executor_id"] == "default"
    )
    assert default_executor["executor"] == "default"
    assert default_executor["executor_pool"] == "default"
    assert default_executor["executor_role"] == "default_shared"
    assert default_executor["logical_pool_count"] == 2
    assert default_executor["logical_pools"] == ["file_resource", "conversation"]
    assert default_executor["dedupe_identity_source"] == "executor_pool"
    assert default_executor["dedupe_confident"] is True
    assert default_executor["max_workers"] == 8
    assert default_executor["work_queue_size"] == 5
    assert "0xabc123" not in str(snapshot)


def test_runtime_concurrency_health_snapshot_keeps_isolated_executors_separate_and_bounded() -> None:
    blocking_scheduler = {
        "pools": {
            f"pool-{index}": {
                "submitted": 1,
                "pending": 0,
                "active": 1,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "operations": {},
            }
            for index in range(5)
        },
        "executors": {
            f"pool-{index}": {
                "max_workers": 1,
                "isolated": True,
                "executor_pool": f"pool-{index}",
                "created": True,
                "runtime": {
                    "executor_id": f"0x{index + 1:04x}",
                    "max_workers": 1,
                    "work_queue_size": index,
                    "thread_count": 1,
                    "alive_thread_count": 1,
                    "shutdown": False,
                    "broken": None,
                },
            }
            for index in range(5)
        },
    }

    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={"status": "ok", "monitor_running": True},
        network_snapshot={},
        runtime_task_summary=_empty_runtime_task_summary(),
        blocking_scheduler_snapshot=blocking_scheduler,
        top_limit=2,
    )

    blocking = snapshot["blocking_scheduler"]
    assert blocking["executor_count"] == 5
    assert blocking["physical_executor_count"] == 5
    assert blocking["physical_executor_totals"]["executor_max_workers"] == 5
    assert blocking["physical_executor_totals"]["executor_work_queue_size"] == 10
    assert blocking["executor_view"]["shared_executor_count"] == 0
    assert snapshot["signals"]["blocking_scheduler"]["shared_executors_detected"] is False
    assert len(blocking["top_physical_executors"]) == 2
    assert len(blocking["top_queued_executors"]) == 2
    assert all(item["logical_pool_count"] == 1 for item in blocking["top_physical_executors"])
    assert all(item["executor_role"] == "isolated_pool" for item in blocking["top_physical_executors"])
    assert "0x0005" not in str(snapshot)
    assert "0x0004" not in str(snapshot)


def test_runtime_concurrency_health_snapshot_marks_inconsistent_shared_executor_runtime_values() -> None:
    blocking_scheduler = {
        "pools": {
            "alpha": {
                "submitted": 1,
                "pending": 0,
                "active": 1,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "operations": {},
            },
            "beta": {
                "submitted": 1,
                "pending": 0,
                "active": 1,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "operations": {},
            },
        },
        "executors": {
            "alpha": {
                "max_workers": 4,
                "isolated": False,
                "executor_pool": "default",
                "created": True,
                "runtime": {
                    "max_workers": 4,
                    "work_queue_size": 1,
                    "thread_count": 3,
                    "alive_thread_count": 2,
                    "shutdown": False,
                    "broken": None,
                },
            },
            "beta": {
                "max_workers": 4,
                "isolated": False,
                "executor_pool": "default",
                "created": True,
                "runtime": {
                    "max_workers": 4,
                    "work_queue_size": 3,
                    "thread_count": 4,
                    "alive_thread_count": 4,
                    "shutdown": True,
                    "broken": "executor broken marker",
                },
            },
        },
    }

    snapshot = runtime_concurrency_health_snapshot(
        event_loop_snapshot={"status": "ok", "monitor_running": True},
        network_snapshot={},
        runtime_task_summary=_empty_runtime_task_summary(),
        blocking_scheduler_snapshot=blocking_scheduler,
        top_limit=5,
    )

    blocking = snapshot["blocking_scheduler"]
    assert blocking["physical_executor_count"] == 1
    assert blocking["physical_executor_totals"]["executor_work_queue_size"] == 3
    assert blocking["physical_executor_totals"]["executor_thread_count"] == 4
    assert blocking["physical_executor_totals"]["executor_alive_thread_count"] == 4
    assert blocking["physical_executor_totals"]["executor_shutdown_count"] == 1
    assert blocking["physical_executor_totals"]["executor_broken_count"] == 1
    assert snapshot["signals"]["blocking_scheduler"]["executor_unavailable"] is True
    assert snapshot["signals"]["blocking_scheduler"]["physical_executor_unavailable"] is True
    assert snapshot["status"] == "degraded"
    physical_executor = blocking["top_physical_executors"][0]
    assert physical_executor["inconsistent_runtime_values"] is True
    assert physical_executor["logical_pools"] == ["alpha", "beta"]
    assert "executor broken marker" not in str(snapshot)


class _LacpRegistrationRegistryLifecycleStub:
    def __init__(self) -> None:
        self.disconnected = False

    def desired_enabled(self) -> list[object]:
        return []

    def mark_disconnected(self) -> None:
        self.disconnected = True


def _mcp_definition(service_id: str = "svc_test") -> McpServiceDefinition:
    return McpServiceDefinition(
        service_id=service_id,
        name="Test MCP",
        transport=StdioTransportConfig(command="python"),
        enabled=True,
    )


def test_stage4_lacp_run_loop_and_websocket_reader_are_registry_managed(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(lacp_client_module, "get_runtime_task_registry", lambda: registry)

        endpoint = "wss://relay.example.test:9443/team/live?token=secret"
        config = LacpConfig(
            enabled=True,
            registry_url=endpoint,
            api_key="secret-api-key",
            reconnect_initial_delay_seconds=0.01,
            reconnect_max_delay_seconds=0.01,
        )
        registration_registry = _LacpRegistrationRegistryLifecycleStub()
        client = LacpClient(
            config_accessor=lambda: config,
            registration_registry=registration_registry,
        )
        connected = asyncio.Event()
        reader_started = asyncio.Event()

        async def resolve_ws_endpoint(_config: LacpConfig) -> str:
            return endpoint

        class FakeWebSocket:
            async def close(self) -> None:
                return None

        class FakeConnect:
            async def __aenter__(self) -> FakeWebSocket:
                connected.set()
                return FakeWebSocket()

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        def fake_connect(ws_endpoint: str, **_kwargs) -> FakeConnect:
            assert ws_endpoint == endpoint
            return FakeConnect()

        async def reader_loop(_ws: object, *, endpoint_owner_id: str) -> None:
            assert endpoint_owner_id == _safe_ws_owner_id(endpoint)
            reader_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_resolve_ws_endpoint", resolve_ws_endpoint)
        monkeypatch.setattr(client, "_reader_loop", reader_loop)
        monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))

        await client.start()
        await asyncio.wait_for(connected.wait(), timeout=1)
        await asyncio.wait_for(reader_started.wait(), timeout=1)

        snapshot = registry.snapshot(include_completed=False)
        assert any(
            record["owner_type"] == "lacp_client"
            and record["owner_id"] == "global"
            and record["task_class"] == "client_run_loop"
            for record in snapshot["tasks"]
        )
        reader_records = [record for record in snapshot["tasks"] if record["task_class"] == "websocket_reader"]
        assert len(reader_records) == 1
        assert reader_records[0]["owner_id"] == "relay.example.test-9443-team-live"
        assert "secret" not in str(snapshot).lower()
        assert "token" not in str(snapshot).lower()

        await client.shutdown()
        await asyncio.sleep(0)

        assert client.status().running is False
        assert registration_registry.disconnected is True

    asyncio.run(run_case())


def test_stage4_mcp_reconnect_loop_is_registry_managed_and_cancellable(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(mcp_session_manager_module, "get_runtime_task_registry", lambda: registry)
        manager = McpSessionManager()
        definition = _mcp_definition("svc_reconnect")
        started = asyncio.Event()

        async def reconnect_loop(_definition: McpServiceDefinition) -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(manager, "_reconnect_loop", reconnect_loop)

        manager.schedule_reconnect(definition)
        await asyncio.wait_for(started.wait(), timeout=1)

        active = registry.snapshot(include_completed=False)
        records = [record for record in active["tasks"] if record["task_class"] == "reconnect_loop"]
        assert len(records) == 1
        assert records[0]["owner_type"] == "mcp_session_manager"
        assert records[0]["owner_id"] == "svc_reconnect"

        await manager.cancel_reconnect(definition.service_id)
        await asyncio.sleep(0)

        assert manager.get_connection_status(definition.service_id).next_retry_at is None
        assert registry.snapshot()["by_status"]["cancelled"] == 1

    asyncio.run(run_case())


def test_stage4_mcp_connection_owner_is_registry_managed_and_disconnect_stops_owner(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(mcp_session_manager_module, "get_runtime_task_registry", lambda: registry)
        manager = McpSessionManager()
        definition = _mcp_definition("svc_owner")
        owner_started = asyncio.Event()
        owner_closed = asyncio.Event()

        async def connection_owner(
            _definition: McpServiceDefinition,
            close_event: asyncio.Event,
            ready_future: asyncio.Future[tuple[object, McpServiceCapabilities]],
            *,
            owner_task_created_at: float,
        ) -> None:
            assert owner_task_created_at > 0
            owner_started.set()
            if not ready_future.done():
                ready_future.set_result(
                    (
                        object(),
                        McpServiceCapabilities(server_name="fake-mcp", server_version="1.0"),
                    )
                )
            await close_event.wait()
            owner_closed.set()

        monkeypatch.setattr(manager, "_connection_owner", connection_owner)

        capabilities = await manager.connect(definition)
        await asyncio.wait_for(owner_started.wait(), timeout=1)

        assert capabilities.server_name == "fake-mcp"
        active = registry.snapshot(include_completed=False)
        records = [record for record in active["tasks"] if record["task_class"] == "connection_owner"]
        assert len(records) == 1
        assert records[0]["owner_type"] == "mcp_session_manager"
        assert records[0]["owner_id"] == "svc_owner"

        await manager.disconnect(definition.service_id)
        await asyncio.wait_for(owner_closed.wait(), timeout=1)

        status = manager.get_connection_status(definition.service_id)
        assert status.status == MCP_CONNECTION_STATUS_DISCONNECTED
        assert manager.is_connected(definition.service_id) is False
        assert registry.snapshot()["by_status"]["completed"] == 1

    asyncio.run(run_case())


def _record_by_class(snapshot: dict[str, object], task_class: str) -> dict[str, object]:
    records = [record for record in snapshot["tasks"] if record["task_class"] == task_class]
    assert len(records) == 1
    return records[0]


def test_stage7_lacp_lifecycle_diagnostics_are_task_correlated_and_sanitized(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(lacp_client_module, "get_runtime_task_registry", lambda: registry)

        endpoint = "wss://user:secret@relay.example.test:9443/team/live?token=secret-token&api_key=hidden"
        config = LacpConfig(
            enabled=True,
            registry_url=endpoint,
            api_key="secret-api-key",
            reconnect_initial_delay_seconds=0.01,
            reconnect_max_delay_seconds=0.01,
            request_timeout_seconds=0.01,
        )
        registration_registry = _LacpRegistrationRegistryLifecycleStub()
        client = LacpClient(config_accessor=lambda: config, registration_registry=registration_registry)
        connected = asyncio.Event()
        reader_started = asyncio.Event()

        class FakeWebSocket:
            def __aiter__(self) -> "FakeWebSocket":
                return self

            async def __anext__(self) -> str:
                reader_started.set()
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def send(self, _message: str) -> None:
                return None

            async def close(self) -> None:
                return None

        class FakeConnect:
            async def __aenter__(self) -> FakeWebSocket:
                connected.set()
                return FakeWebSocket()

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        async def resolve_ws_endpoint(_config: LacpConfig) -> str:
            return endpoint

        def fake_connect(ws_endpoint: str, **_kwargs) -> FakeConnect:
            assert ws_endpoint == endpoint
            return FakeConnect()

        monkeypatch.setattr(client, "_resolve_ws_endpoint", resolve_ws_endpoint)
        monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))

        await client.start()
        await asyncio.wait_for(connected.wait(), timeout=1)
        await asyncio.wait_for(reader_started.wait(), timeout=1)

        active = registry.snapshot(include_completed=False)
        run_record = _record_by_class(active, "client_run_loop")
        reader_record = _record_by_class(active, "websocket_reader")
        run_context = run_record["diagnostic_context"]
        reader_context = reader_record["diagnostic_context"]
        assert run_context["phase"] == "reader_task.created"
        assert run_context["task_id"] == run_record["task_id"]
        assert run_context["reader_task_id"] == reader_record["task_id"]
        assert run_context["endpoint_owner_id"] == "relay.example.test-9443-team-live"
        assert reader_context["phase"] == "reader.waiting"
        assert reader_context["task_id"] == reader_record["task_id"]
        assert reader_context["endpoint_owner_id"] == "relay.example.test-9443-team-live"
        assert "secret" not in str(active).lower()
        assert "token" not in str(active).lower()
        assert "api_key" not in str(active).lower()

        await client.shutdown()
        await asyncio.sleep(0)

        completed = registry.snapshot()
        reader_completed = _record_by_class(completed, "websocket_reader")
        run_completed = _record_by_class(completed, "client_run_loop")
        assert reader_completed["diagnostic_context"]["phase"] == "reader.exit"
        assert reader_completed["diagnostic_context"]["reason"] in {"reader_cancelled", "reader_completed"}
        assert run_completed["diagnostic_context"]["phase"] in {"client_run_loop.cancelled", "websocket.disconnected"}
        assert "secret" not in str(completed).lower()
        assert "token" not in str(completed).lower()
        assert "api_key" not in str(completed).lower()

    asyncio.run(run_case())


def test_stage7_lacp_pending_request_failure_reasons_are_bounded(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(lacp_client_module, "get_runtime_task_registry", lambda: registry)

        config = LacpConfig(
            enabled=True,
            registry_url="wss://relay.example.test/ws",
            api_key="secret-api-key",
            request_timeout_seconds=0.01,
        )
        registration_registry = _LacpRegistrationRegistryLifecycleStub()
        client = LacpClient(config_accessor=lambda: config, registration_registry=registration_registry)

        async def run_loop_marker() -> None:
            await asyncio.Event().wait()

        run_task = registry.create_task(
            run_loop_marker(),
            task_name="lacp-client-run-loop",
            owner_type="lacp_client",
            owner_id="global",
            task_class="client_run_loop",
        )
        client._task = run_task

        try:
            try:
                await client._send_request({"type": "register"}, expected_type="register_ack")
            except RuntimeError as exc:
                assert str(exc) == "LACP WebSocket is not connected."
            disconnected_context = _record_by_class(registry.snapshot(include_completed=False), "client_run_loop")["diagnostic_context"]
            assert disconnected_context["phase"] == "pending_request.failed"
            assert disconnected_context["reason"] == "websocket_disconnected"
            assert disconnected_context["expected_type"] == "register_ack"
            assert disconnected_context["error_type"] == "RuntimeError"

            class TimeoutWebSocket:
                async def send(self, _message: str) -> None:
                    return None

            client._ws = TimeoutWebSocket()
            try:
                await client._send_request({"type": "register"}, expected_type="register_ack")
            except TimeoutError as exc:
                assert str(exc) == "LACP request timed out."
            timeout_context = _record_by_class(registry.snapshot(include_completed=False), "client_run_loop")["diagnostic_context"]
            assert timeout_context["phase"] == "pending_request.failed"
            assert timeout_context["reason"] == "request_timeout"
            assert timeout_context["expected_type"] == "register_ack"
            assert timeout_context["timeout_seconds"] == 0.01
            assert timeout_context["error_type"] == "TimeoutError"

            async def fake_send_json(frame: dict[str, object]) -> None:
                request_id = frame["request_id"]
                future = client._pending_requests.pop(request_id)
                unexpected_type = "unexpected-" + "x" * 200
                future.set_result({"type": unexpected_type, "payload": "secret-token-raw-payload"})

            client._ws = object()
            monkeypatch.setattr(client, "_send_json", fake_send_json)
            try:
                await client._send_request({"type": "register"}, expected_type="register_ack")
            except RuntimeError as exc:
                assert str(exc) == "Unexpected LACP response type: " + ("unexpected-" + "x" * 200)[:80]
            unexpected_context = _record_by_class(registry.snapshot(include_completed=False), "client_run_loop")["diagnostic_context"]
            assert unexpected_context["phase"] == "pending_request.failed"
            assert unexpected_context["reason"] == "unexpected_response_type"
            assert unexpected_context["expected_type"] == "register_ack"
            assert unexpected_context["response_type"] == ("unexpected-" + "x" * 200)[:80]
            assert unexpected_context["error_type"] == "RuntimeError"
            assert "secret-token-raw-payload" not in str(unexpected_context)

            future = asyncio.get_running_loop().create_future()
            client._pending_requests["pending-secret-request-id"] = future
            client._fail_pending(RuntimeError("LACP client stopped."), reason="client_stopped", task=run_task)
            stopped_context = _record_by_class(registry.snapshot(include_completed=False), "client_run_loop")["diagnostic_context"]
            assert stopped_context["phase"] == "pending_requests.failed"
            assert stopped_context["reason"] == "client_stopped"
            assert stopped_context["pending_request_count"] == 1
            assert stopped_context["error_type"] == "RuntimeError"
            assert client._pending_requests == {}
        finally:
            registry.cancel(run_task, message="test cleanup")
            with suppress(asyncio.CancelledError):
                await run_task

    asyncio.run(run_case())


def test_stage7_mcp_ready_owner_and_reconnect_diagnostics_are_task_correlated(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        progress_contexts: list[dict[str, object]] = []
        original_mark_progress = registry.mark_progress

        def record_mark_progress(
            task_or_id: object,
            *,
            diagnostic_context: dict[str, object] | None = None,
            **kwargs: object,
        ) -> None:
            if diagnostic_context is not None:
                progress_contexts.append(dict(diagnostic_context))
            original_mark_progress(task_or_id, diagnostic_context=diagnostic_context, **kwargs)

        registry.mark_progress = record_mark_progress
        monkeypatch.setattr(mcp_session_manager_module, "get_runtime_task_registry", lambda: registry)

        manager = McpSessionManager()
        definition = _mcp_definition("svc_stage7")

        class FakeSession:
            async def initialize(self) -> SimpleNamespace:
                return SimpleNamespace(
                    capabilities=SimpleNamespace(tools=object(), resources=None),
                    serverInfo=SimpleNamespace(name="stage7", version="1.0"),
                    instructions="bounded",
                )

        async def create_session(_definition: McpServiceDefinition, _exit_stack: object) -> FakeSession:
            return FakeSession()

        monkeypatch.setattr(manager, "_create_session", create_session)

        capabilities = await manager.connect(definition)
        assert capabilities.supports_tools is True
        active = registry.snapshot(include_completed=False)
        await asyncio.sleep(0)
        owner_record = _record_by_class(active, "connection_owner")
        owner_context = owner_record["diagnostic_context"]
        assert owner_context["phase"] in {"connection.connected", "close_event.waiting"}
        assert owner_context["service_id"] == "svc_stage7"
        assert owner_context["transport_type"] == "StdioTransportConfig"
        assert owner_context["task_id"] == owner_record["task_id"]
        owner_task_id = owner_record["task_id"]
        owner_contexts = [context for context in progress_contexts if context.get("service_id") == "svc_stage7"]
        owner_phases = {context.get("phase") for context in owner_contexts}
        assert "owner_task.created" in owner_phases
        assert "ready_future.waiting" in owner_phases
        assert "ready_future.completed" in owner_phases
        assert "owner_task.started" in owner_phases
        assert "ready_future.set_result" in owner_phases
        assert "close_event.waiting" in owner_phases
        assert all(context.get("task_id") == owner_task_id for context in owner_contexts if "task_id" in context)
        assert "secret" not in str(active).lower()
        assert "traceback" not in str(active).lower()

        await manager.disconnect(definition.service_id)
        await asyncio.sleep(0)
        owner_completed = _record_by_class(registry.snapshot(), "connection_owner")
        assert owner_completed["diagnostic_context"]["phase"] == "owner_task.cleanup.completed"
        assert owner_completed["diagnostic_context"]["reason"] == "exit_stack_closed"

        reconnect_definition = _mcp_definition("svc_reconnect_stage7")
        reconnect_state = manager._state(reconnect_definition.service_id)
        reconnect_state.retry_attempt = 1
        reconnect_started = asyncio.Event()

        async def failing_connect(_definition: McpServiceDefinition, *, status: str) -> None:
            reconnect_started.set()
            raise RuntimeError("secret token raw failure")

        async def no_sleep(_delay: float) -> None:
            reconnect_definition.enabled = False

        monkeypatch.setattr(manager, "_connect", failing_connect)
        monkeypatch.setattr(mcp_session_manager_module.asyncio, "sleep", no_sleep)
        reconnect_task = registry.create_task(
            manager._reconnect_loop(reconnect_definition),
            task_name="mcp-reconnect-loop-svc_reconnect_stage7",
            owner_type="mcp_session_manager",
            owner_id=reconnect_definition.service_id,
            task_class="reconnect_loop",
        )
        await asyncio.wait_for(reconnect_started.wait(), timeout=1)
        await reconnect_task
        reconnect_record = _record_by_class(registry.snapshot(), "reconnect_loop")
        reconnect_context = reconnect_record["diagnostic_context"]
        assert reconnect_context["phase"] == "reconnect_loop.connect_failed"
        assert reconnect_context["reason"] == "connect_exception"
        assert reconnect_context["service_id"] == "svc_reconnect_stage7"
        assert reconnect_context["transport_type"] == "StdioTransportConfig"
        assert reconnect_context["task_id"] == reconnect_record["task_id"]
        assert reconnect_context["reconnect_attempt"] == 1
        assert reconnect_context["error_type"] == "RuntimeError"
        assert "secret token raw failure" not in str(reconnect_context)

    asyncio.run(run_case())


def test_stage8_agent_execution_background_task_is_registry_managed(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(agent_execution_engine_module, "get_runtime_task_registry", lambda: registry)

        task = AgentTask(
            task_id="task-stage8",
            root_agent_instance_id="instance-stage8",
            agent_instance_ids=("instance-stage8",),
        )
        instance = AgentInstance(
            instance_id="instance-stage8",
            task_id=task.task_id,
            thread_id="thread-stage8",
            current_working_directory="D:/project/agent",
            agent_definition_id="coding",
            llm_provider_id="provider-stage8",
            llm_model_binding_id="binding-stage8",
            conversation_id="conversation-stage8",
            trace_id="trace-stage8",
            status=AGENT_INSTANCE_STATUS_READY,
        )

        class RuntimeGateway:
            def __init__(self) -> None:
                self.instance = instance
                self.task = task

            def get_agent_instance(self, instance_id: str) -> AgentInstance:
                assert instance_id == self.instance.instance_id
                return self.instance

            def get_task(self, task_id: str) -> AgentTask:
                assert task_id == self.task.task_id
                return self.task

            def enqueue_agent_instance_user_message(self, instance_id: str, **kwargs: object):
                pending = PendingAgentUserMessage(
                    message_id="pending-stage8",
                    user_text=str(kwargs["user_text"]),
                    client_message_id=kwargs.get("client_message_id"),
                    metadata=dict(kwargs.get("metadata") or {}),
                )
                self.instance = replace(self.instance, pending_user_messages=(pending,))
                return self.instance, pending

        engine = object.__new__(AgentExecutionEngine)
        engine._runtime_gateway = RuntimeGateway()
        engine._communication_gateway = None
        engine._background_tasks = set()
        engine._active_instance_ids = set()
        engine._active_execution_handles = {}
        engine._pause_timeout_handles = {}
        engine._execution_progress = {}
        engine._record_execution_instance_event = lambda **_kwargs: None
        engine._validate_file_input_before_enqueue = lambda _instance, _attachments: asyncio.sleep(0)

        execution_started = asyncio.Event()
        release_execution = asyncio.Event()

        async def run_agent_instance(_task, _instance, _request, *, interrupt_state):
            execution_started.set()
            await release_execution.wait()
            return AgentExecutionResult(
                task_id=_task.task_id,
                instance_id=_instance.instance_id,
                conversation_id=_instance.conversation_id or "conversation-stage8",
                trace_file_path="trace.md",
                assistant_message=None,
                communication_stream_id=_request.communication_stream_id,
            )

        engine._run_agent_instance = run_agent_instance

        result = await engine.submit_agent_instance_message(
            agent_instance_id="instance-stage8",
            user_text="hello",
            communication_stream_id="stream-stage8",
            client_message_id="client-stage8",
        )
        await asyncio.wait_for(execution_started.wait(), timeout=1)

        assert result.status == "accepted"
        assert result.queued_for_later is False
        assert result.pending_message_id == "pending-stage8"
        assert result.communication_stream_id == "stream-stage8"
        assert "instance-stage8" in engine._active_instance_ids
        assert "instance-stage8" in engine._active_execution_handles
        [task_handle] = list(engine._background_tasks)
        assert engine._active_execution_handles["instance-stage8"].task is task_handle

        active = registry.snapshot(include_completed=False)
        record = _record_by_class(active, "agent_instance_execution")
        assert record["task_name"] == "agent-execution-instance-stage8"
        assert record["owner_type"] == "agent_execution_engine"
        assert record["owner_id"] == "instance-stage8"
        context = record["diagnostic_context"]
        assert context == {
            "phase": "background_execution.launch",
            "agent_task_id": "task-stage8",
            "instance_id": "instance-stage8",
            "conversation_id": "conversation-stage8",
            "communication_stream_id": "stream-stage8",
            "agent_definition_id": "coding",
            "queued_for_later": False,
            "active_instance_count": 1,
            "interrupt_requested": False,
        }

        release_execution.set()
        await asyncio.wait_for(task_handle, timeout=1)
        await asyncio.sleep(0)
        assert engine._background_tasks == set()
        assert engine._active_instance_ids == set()
        assert engine._active_execution_handles == {}
        assert registry.snapshot()["by_status"]["completed"] == 1

    asyncio.run(run_case())


def test_stage8_scene_detach_terminal_cleanup_is_registry_managed(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(agent_runtime_gateway_impl_module, "get_runtime_task_registry", lambda: registry)

        deleted_task_ids: list[str] = []

        class TerminalGateway:
            async def delete_task_terminals(self, task_id: str) -> list[object]:
                deleted_task_ids.append(task_id)
                return [object(), object()]

        monkeypatch.setitem(
            sys.modules,
            "agent_runtime.terminal.terminal_factory",
            SimpleNamespace(TerminalFactory=SimpleNamespace(create=lambda: TerminalGateway())),
        )

        agent_runtime_gateway_impl_module._delete_task_terminals("task-stage8-scene")
        active = registry.snapshot(include_completed=False)
        record = _record_by_class(active, "scene_detach_terminal_cleanup")
        assert record["task_name"] == "scene-detach-terminal-cleanup-task-stage8-scene"
        assert record["owner_type"] == "agent_runtime_gateway"
        assert record["owner_id"] == "task-stage8-scene"
        assert record["diagnostic_context"] == {
            "phase": "scene_detach_terminal_cleanup.scheduled",
            "task_id": "task-stage8-scene",
            "agent_task_id": "task-stage8-scene",
            "target_task_id": "task-stage8-scene",
            "reason": "scene_detach",
        }

        [cleanup_task] = [task for task in asyncio.all_tasks() if task.get_name() == record["task_name"]]
        await asyncio.wait_for(cleanup_task, timeout=1)
        await asyncio.sleep(0)

        completed = _record_by_class(registry.snapshot(), "scene_detach_terminal_cleanup")
        assert deleted_task_ids == ["task-stage8-scene"]
        assert completed["status"] == "completed"
        assert completed["diagnostic_context"] == {
            "phase": "scene_detach_terminal_cleanup.completed",
            "task_id": "task-stage8-scene",
            "agent_task_id": "task-stage8-scene",
            "target_task_id": "task-stage8-scene",
            "reason": "scene_detach",
            "deleted_terminal_count": 2,
        }
        assert "command" not in str(completed).lower()
        assert "terminal output" not in str(completed).lower()

    asyncio.run(run_case())


def test_stage8_llm_runtime_event_publish_same_loop_is_registry_managed_and_bounded(monkeypatch) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(llm_request_service_module, "get_runtime_task_registry", lambda: registry)
        published: list[tuple[object, str, dict[str, object], str | None, dict[str, object] | None]] = []

        class Publisher:
            async def publish(self, context, event_type, payload, *, source=None, metadata=None, **_kwargs):
                published.append((context, event_type, payload, source, metadata))

        context = RuntimeCommunicationContext(stream_id="stream-stage8")
        payload = {
            "delta": "secret raw LLM delta text",
            "thinking": "secret raw thinking text",
            "tool_output": "secret raw tool output",
        }
        llm_request_service_module._schedule_runtime_event_publish(
            publisher=Publisher(),
            context=context,
            event_type="llm.response.delta",
            payload=payload,
            event_loop=asyncio.get_running_loop(),
            conversation_id="conversation-stage8",
        )

        active = registry.snapshot(include_completed=False)
        record = _record_by_class(active, "runtime_event_publish")
        assert record["task_name"] == "llm-runtime-event-publish-llm.response.delta"
        assert record["owner_type"] == "llm_request_service"
        assert record["owner_id"] == "conversation-stage8"
        scheduled_context = record["diagnostic_context"]
        assert scheduled_context["phase"] == "runtime_event_publish.scheduled"
        assert scheduled_context["event_type"] == "llm.response.delta"
        assert scheduled_context["conversation_id"] == "conversation-stage8"
        assert scheduled_context["source"] == "llm_client"
        assert scheduled_context["payload_keys"] == "[redacted]"
        assert scheduled_context["payload_key_count"] == "[redacted]"
        assert scheduled_context["payload_size"] == "[redacted]"
        assert "secret raw" not in str(scheduled_context)

        [publish_task] = [task for task in asyncio.all_tasks() if task.get_name() == record["task_name"]]
        await asyncio.wait_for(publish_task, timeout=1)
        await asyncio.sleep(0)
        assert published == [(context, "llm.response.delta", payload, "llm_client", {"conversation_id": "conversation-stage8"})]
        assert registry.snapshot()["by_status"]["completed"] == 1

    asyncio.run(run_case())


def test_stage8_llm_runtime_event_publish_failure_diagnostics_are_bounded(monkeypatch, caplog) -> None:
    async def run_case() -> None:
        registry = RuntimeTaskRegistry(completed_task_retention=8)
        monkeypatch.setattr(llm_request_service_module, "get_runtime_task_registry", lambda: registry)

        class Publisher:
            async def publish(self, *_args, **_kwargs):
                raise RuntimeError("secret raw provider payload should not leak")

        payload = {
            "delta": "secret raw LLM delta text",
            "payload": "secret raw payload value",
        }
        with caplog.at_level(logging.ERROR):
            llm_request_service_module._schedule_runtime_event_publish(
                publisher=Publisher(),
                context=RuntimeCommunicationContext(stream_id="stream-stage8"),
                event_type="llm.response.delta",
                payload=payload,
                event_loop=asyncio.get_running_loop(),
                conversation_id="conversation-stage8",
            )
            record = _record_by_class(registry.snapshot(include_completed=False), "runtime_event_publish")
            [publish_task] = [task for task in asyncio.all_tasks() if task.get_name() == record["task_name"]]
            with suppress(RuntimeError):
                await asyncio.wait_for(publish_task, timeout=1)
            await asyncio.sleep(0)

        failed = _record_by_class(registry.snapshot(), "runtime_event_publish")
        assert failed["status"] == "failed"
        assert failed["last_error_type"] == "RuntimeError"
        assert failed["last_error"] == "Runtime event publish failed."
        failure_context = failed["diagnostic_context"]
        assert failure_context["phase"] == "runtime_event_publish.failed"
        assert failure_context["event_type"] == "llm.response.delta"
        assert failure_context["conversation_id"] == "conversation-stage8"
        assert failure_context["source"] == "llm_client"
        assert failure_context["payload_keys"] == "[redacted]"
        assert failure_context["payload_key_count"] == "[redacted]"
        assert failure_context["payload_size"] == "[redacted]"
        assert failure_context["error_type"] == "RuntimeError"
        diagnostic_text = str(failed).lower()
        log_text = caplog.text.lower()
        assert "secret raw" not in diagnostic_text
        assert "provider payload" not in diagnostic_text
        assert "secret raw" not in log_text
        assert "provider payload" not in log_text
        assert "traceback" not in log_text

    asyncio.run(run_case())
