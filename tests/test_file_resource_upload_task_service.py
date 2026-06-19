import asyncio

from agent_runtime.file_resource.upload_task_service import FileResourceUploadTask, FileResourceUploadTaskService


def _upload_task() -> FileResourceUploadTask:
    return FileResourceUploadTask(task_id="file-upload-test", resources=[], names=[])


def _task_service() -> FileResourceUploadTaskService:
    return FileResourceUploadTaskService(object())  # type: ignore[arg-type]


def test_wait_for_event_async_returns_false_on_timeout_and_removes_waiter() -> None:
    service = _task_service()
    task = _upload_task()

    async def run_case() -> None:
        result = await service.wait_for_event_async(task, after_seq=0, timeout_seconds=0.01)

        assert result is False
        assert task.async_waiters == {}

    asyncio.run(run_case())


def test_wait_for_event_async_wakes_on_later_event_and_preserves_after_seq() -> None:
    service = _task_service()
    task = _upload_task()

    async def run_case() -> None:
        waiter = asyncio.create_task(service.wait_for_event_async(task, after_seq=0, timeout_seconds=1.0))
        await asyncio.sleep(0)

        assert len(task.async_waiters) == 1

        service._publish(
            task,
            status="running",
            phase="processing",
            message="processing",
            progress_percent=10,
        )

        assert await asyncio.wait_for(waiter, timeout=1.0) is True
        assert task.async_waiters == {}
        assert [event.seq for event in service.list_events_after(task, 0)] == [1]
        assert service.list_events_after(task, 1) == []

    asyncio.run(run_case())


def test_wait_for_event_async_removes_waiter_on_await_cancellation() -> None:
    service = _task_service()
    task = _upload_task()

    async def run_case() -> None:
        waiter = asyncio.create_task(service.wait_for_event_async(task, after_seq=0, timeout_seconds=30.0))
        await asyncio.sleep(0)

        assert len(task.async_waiters) == 1

        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        assert task.async_waiters == {}

    asyncio.run(run_case())


def test_wait_for_event_async_wakes_on_terminal_status_without_newer_seq_requirement() -> None:
    service = _task_service()
    task = _upload_task()

    async def run_case() -> None:
        waiter = asyncio.create_task(service.wait_for_event_async(task, after_seq=99, timeout_seconds=1.0))
        await asyncio.sleep(0)

        assert len(task.async_waiters) == 1

        service._publish(
            task,
            status="completed",
            phase="completed",
            message="done",
            progress_percent=100,
        )

        assert await asyncio.wait_for(waiter, timeout=1.0) is True
        assert task.async_waiters == {}

    asyncio.run(run_case())
