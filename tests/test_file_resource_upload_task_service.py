import asyncio
import json

from agent_runtime.file_resource.north_gateway.file_resource_rest_adapter import _format_upload_task_sse
from agent_runtime.file_resource.upload_task_service import (
    FileResourceUploadTask,
    FileResourceUploadTaskEvent,
    FileResourceUploadTaskService,
)


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


from pathlib import Path
import tempfile
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.file_resource.north_gateway.file_resource_rest_adapter import (
    _format_upload_task_sse,
    create_file_resource_router,
)


def test_file_upload_task_sse_is_resource_scoped_and_legally_has_no_agent_owner() -> None:
    event = FileResourceUploadTaskEvent(
        task_id="file-upload-1",
        seq=1,
        status="running",
        phase="uploading",
        message="uploading",
        progress_percent=50,
    )
    frame = _format_upload_task_sse(event)
    assert frame.startswith("id: 1\nevent: file_resource_upload_task\n")
    payload = json.loads(frame.splitlines()[2].removeprefix("data: "))
    assert payload["task_id"] == "file-upload-1"
    assert "agent_instance_id" not in payload


def test_get_local_image_content_serves_valid_local_image_and_rejects_invalid_paths() -> None:
    app = FastAPI()
    app.include_router(create_file_resource_router(), prefix="/api/file-resources")
    client = TestClient(app)
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        image_path = tmp_root / "sample.png"
        image_path.write_bytes(png_bytes)

        text_path = tmp_root / "notes.txt"
        text_path.write_text("not an image", encoding="utf-8")

        res_ok = client.get("/api/file-resources/local-image/content", params={"path": str(image_path)})
        assert res_ok.status_code == 200
        assert res_ok.headers["content-type"].startswith("image/png")
        assert res_ok.content == png_bytes

        res_relative = client.get("/api/file-resources/local-image/content", params={"path": "relative/sample.png"})
        assert res_relative.status_code == 400

        res_missing = client.get("/api/file-resources/local-image/content", params={"path": str(tmp_root / "missing.png")})
        assert res_missing.status_code == 404

        res_non_image = client.get("/api/file-resources/local-image/content", params={"path": str(text_path)})
        assert res_non_image.status_code == 400

