# -*- coding: utf-8 -*-
from agent_runtime.task_note.task_note_snapshot_service import (
    TaskNoteSnapshotService,
    get_empty_task_note_snapshot,
)


def test_empty_snapshot_structure() -> None:
    snapshot = get_empty_task_note_snapshot("task-001", "inst-001")
    assert snapshot["task_id"] == "task-001"
    assert snapshot["agent_instance_id"] == "inst-001"
    assert snapshot["missions"] == []
    assert snapshot["workspace_context"] == []
    assert snapshot["solution"] is None
    assert snapshot["decisions"] == []
    assert snapshot["todos"] == []
    assert snapshot["progress"] is None
    assert snapshot["discoveries"] == []
    assert snapshot["issues"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["validations"] == []
    assert snapshot["external_states"] == []
    assert snapshot["commands"] == []
    assert snapshot["agent_relationships"] == []
    assert snapshot["agent_messages"] == []
    assert snapshot["remote_agent_messages"] == []


def test_snapshot_service_without_db() -> None:
    service = TaskNoteSnapshotService(connection_factory=None)
    snapshot = service.build_snapshot(task_id="task-001", agent_instance_id="inst-001")
    assert snapshot["task_id"] == "task-001"
    assert snapshot["agent_instance_id"] == "inst-001"
    assert isinstance(snapshot["todos"], list)
