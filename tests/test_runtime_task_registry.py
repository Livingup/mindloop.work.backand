import asyncio
import logging

from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry


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
