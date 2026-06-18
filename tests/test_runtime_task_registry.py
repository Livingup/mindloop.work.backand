import asyncio
import logging
import sys
from contextlib import suppress
from types import SimpleNamespace

from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry, runtime_task_registry_admission_policy
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
from agent_runtime.runtime_health import runtime_concurrency_health_snapshot, runtime_task_health_summary


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

    assert snapshot["status"] == "degraded"
    assert snapshot["diagnostic_only"] is True
    assert snapshot["signals"]["event_loop"]["lagging"] is True
    assert snapshot["signals"]["runtime_tasks"]["stale_progress"] is True
    assert snapshot["signals"]["network"]["event_hub_queue_pressure"] is True
    assert snapshot["blocking_scheduler"]["totals"]["pending"] == 2
    assert snapshot["blocking_scheduler"]["totals"]["executor_work_queue_size"] == 2
    assert len(snapshot["blocking_scheduler"]["top_active_pools"]) == 1
    assert len(snapshot["runtime_tasks"]["top_active_owners"]) == 1
    assert "top_operations" not in snapshot["blocking_scheduler"]
    payload_text = str(snapshot).lower()
    assert "diagnostic_context" not in payload_text
    assert "should-not-appear" not in payload_text
    assert "authorization" not in payload_text
    assert "password" not in payload_text
    assert "token" not in payload_text


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
    assert set(operation) == {"pool", "operation", "submitted", "pending", "active", "completed", "failed", "cancelled"}



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
