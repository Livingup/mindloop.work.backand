# -*- coding: utf-8 -*-
"""Task Note 客观物理事实程序自动记录器及工具钩子单元测试。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.task_note.task_note_event_recorder import (
    TaskNoteEventRecorder,
    get_task_note_event_recorder,
    set_task_note_event_recorder,
)
from agent_runtime.terminal.north_gateway.terminal_contracts import ExecuteTerminalCommandResult
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError
from agent_runtime.terminal.terminal_tool import (
    TerminalExecuteCommandToolRequest,
    terminal_execute_command,
)
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


class MockCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((query.strip(), params or ()))

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    def __enter__(self) -> "MockCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class MockConnection:
    def __init__(self, cursor: MockCursor) -> None:
        self.cursor_obj = cursor
        self.committed = False

    def cursor(self) -> MockCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "MockConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


@pytest.fixture
def mock_db() -> tuple[MockCursor, MockConnection, Any]:
    cur = MockCursor()
    conn = MockConnection(cur)

    @contextmanager
    def factory() -> Iterator[MockConnection]:
        yield conn

    return cur, conn, factory


def test_record_file_artifact(mock_db: Any) -> None:
    cur, conn, factory = mock_db
    recorder = TaskNoteEventRecorder(connection_factory=factory)

    art_id = recorder.record_file_artifact(
        task_id="task-1",
        agent_instance_id="inst-1",
        op_type="modify",
        file_path="/workspace/test.py",
        edits=[{"feature_desc": "fix bug", "deleted_lines": 1, "inserted_lines": 2}],
    )

    assert art_id is not None
    assert art_id.startswith("art_")
    assert conn.committed is True
    assert len(cur.executed) == 1
    query, params = cur.executed[0]
    assert "INSERT INTO task_artifact_records" in query
    assert params[1] == "task-1"
    assert params[2] is None  # mission_id defaults to NULL
    assert params[3] == "inst-1"
    assert params[4] == "modify"
    assert params[5] == "/workspace/test.py"


def test_record_terminal_command(mock_db: Any) -> None:
    cur, conn, factory = mock_db
    recorder = TaskNoteEventRecorder(connection_factory=factory)

    ok = recorder.record_terminal_command(
        task_id="task-1",
        agent_instance_id="inst-1",
        command_id="cmd-12345",
        command_line="npm test",
    )

    assert ok is True
    assert conn.committed is True
    assert len(cur.executed) == 1
    query, params = cur.executed[0]
    assert "INSERT INTO task_command_records" in query
    assert params == ("cmd-12345", "task-1", "inst-1", "npm test")


def test_record_subagent_relationship(mock_db: Any) -> None:
    cur, conn, factory = mock_db
    recorder = TaskNoteEventRecorder(connection_factory=factory)

    rel_id = recorder.record_subagent_relationship(
        task_id="task-1",
        parent_instance_id="inst-parent",
        child_instance_id="inst-child",
    )

    assert rel_id is not None
    assert str(rel_id).startswith("rel_")
    assert conn.committed is True
    # 2 rows: parent view and child view
    assert len(cur.executed) == 2
    q1, p1 = cur.executed[0]
    assert p1[1] == "task-1"
    assert p1[2] is None  # mission_id
    assert p1[3] == "inst-parent"
    assert p1[4] == "child"
    assert p1[5] == "inst-child"
    assert p1[6] is None  # delegation_content
    assert p1[7] is None  # fork_message_id
    assert p1[8] == "unreleased"

    q2, p2 = cur.executed[1]
    assert p2[1] == "task-1"
    assert p2[2] is None  # mission_id
    assert p2[3] == "inst-child"
    assert p2[4] == "parent"
    assert p2[5] == "inst-parent"
    assert p2[6] is None  # delegation_content
    assert p2[7] is None  # fork_message_id
    assert p2[8] == "unreleased"


def test_record_agent_message(mock_db: Any) -> None:
    cur, conn, factory = mock_db
    recorder = TaskNoteEventRecorder(connection_factory=factory)

    ok = recorder.record_agent_message(
        task_id="task-1",
        from_instance_id="inst-parent",
        to_instance_id="inst-child",
        target_agent_type="child",
        message="Please start the task.",
    )

    assert ok is True
    assert conn.committed is True
    # 2 rows: sender (send) and receiver (receive)
    assert len(cur.executed) == 2
    q1, p1 = cur.executed[0]
    assert p1[1] == "task-1"
    assert p1[3] == "inst-parent"
    assert p1[4] == "inst-child"
    assert p1[5] == "child"
    assert p1[6] == "send"
    assert p1[7] == "Please start the task."

    q2, p2 = cur.executed[1]
    assert p2[1] == "task-1"
    assert p2[3] == "inst-child"
    assert p2[4] == "inst-parent"
    assert p2[5] == "parent"
    assert p2[6] == "receive"
    assert p2[7] == "Please start the task."


def test_record_remote_agent_message(mock_db: Any) -> None:
    cur, conn, factory = mock_db
    recorder = TaskNoteEventRecorder(connection_factory=factory)

    ok = recorder.record_remote_agent_message(
        task_id="task-1",
        agent_instance_id="inst-local",
        remote_agent_instance_id="inst-remote",
        message_type="receive",
        message="Hello from peer agent",
    )

    assert ok is True
    assert conn.committed is True
    assert len(cur.executed) == 1
    query, params = cur.executed[0]
    assert "INSERT INTO task_remote_agent_messages" in query
    assert params[1] == "task-1"
    assert params[3] == "inst-local"
    assert params[4] == "inst-remote"
    assert params[5] == "receive"
    assert params[6] == "Hello from peer agent"


def test_safe_tolerance_on_exception() -> None:
    @contextmanager
    def failing_factory() -> Iterator[Any]:
        raise ConnectionError("DB is down")

    recorder = TaskNoteEventRecorder(connection_factory=failing_factory)
    # None of these should raise exceptions
    assert recorder.record_file_artifact(task_id="t", agent_instance_id="i", op_type="create") is None
    assert recorder.record_terminal_command(task_id="t", agent_instance_id="i", command_id="c", command_line="ls") is False
    assert recorder.record_subagent_relationship(task_id="t", parent_instance_id="p", child_instance_id="c") is None
    assert recorder.record_agent_message(task_id="t", from_instance_id="p", to_instance_id="c", target_agent_type="child", message="m") is False
    assert recorder.record_remote_agent_message(task_id="t", agent_instance_id="i", remote_agent_instance_id="r", message_type="send", message="m") is False


# ── Terminal Tool: Command ID Guaranteed Return on Success and Failure ─────

@pytest.mark.asyncio
async def test_terminal_execute_command_success_returns_command_id() -> None:
    mock_recorder = MagicMock()
    mock_recorder.async_record_terminal_command = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    with patch("agent_runtime.terminal.terminal_factory.TerminalFactory.create") as mock_factory:
        mock_gateway = MagicMock()
        mock_factory.return_value = mock_gateway
        mock_gateway.execute_command = AsyncMock(
            return_value=ExecuteTerminalCommandResult(
                accepted=True,
                terminal_id="term-1",
                command_id="cmd-success-123",
                status="running",
                actual_cwd="/workspace",
                shell="powershell.exe",
            )
        )

        ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
        req = TerminalExecuteCommandToolRequest(terminal_id="term-1", command_line="git status")
        res = await terminal_execute_command(req, ctx)

        assert res.success is True
        assert res.execution_status == "success"
        assert res.output["accepted"] is True
        assert res.output["terminal_id"] == "term-1"
        assert res.output["command_id"] == "cmd-success-123"
        mock_recorder.async_record_terminal_command.assert_awaited_once_with(
            task_id="task-1",
            agent_instance_id="inst-1",
            command_id="cmd-success-123",
            command_line="git status",
        )


@pytest.mark.asyncio
async def test_terminal_execute_command_rejected_returns_command_id() -> None:
    mock_recorder = MagicMock()
    mock_recorder.async_record_terminal_command = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    with patch("agent_runtime.terminal.terminal_factory.TerminalFactory.create") as mock_factory:
        mock_gateway = MagicMock()
        mock_factory.return_value = mock_gateway
        mock_gateway.execute_command = AsyncMock(
            return_value=ExecuteTerminalCommandResult(
                accepted=False,
                terminal_id="term-1",
                command_id="cmd-rejected-456",
                status="failed",
                actual_cwd="/workspace",
                shell="powershell.exe",
            )
        )

        ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
        req = TerminalExecuteCommandToolRequest(terminal_id="term-1", command_line="invalid command")
        res = await terminal_execute_command(req, ctx)

        assert res.success is False
        assert res.execution_status == "failed"
        assert res.output["accepted"] is False
        assert res.output["command_id"] == "cmd-rejected-456"
        mock_recorder.async_record_terminal_command.assert_awaited_once_with(
            task_id="task-1",
            agent_instance_id="inst-1",
            command_id="cmd-rejected-456",
            command_line="invalid command",
        )


@pytest.mark.asyncio
async def test_terminal_execute_command_capacity_exceeded_returns_command_id() -> None:
    mock_recorder = MagicMock()
    mock_recorder.async_record_terminal_command = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    with patch("agent_runtime.terminal.terminal_factory.TerminalFactory.create") as mock_factory:
        mock_gateway = MagicMock()
        mock_factory.return_value = mock_gateway
        mock_gateway.execute_command = AsyncMock(
            side_effect=TerminalCapacityExceededError(
                "Too many commands",
                scope="task",
                limit=5,
                current=5,
            )
        )

        ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
        req = TerminalExecuteCommandToolRequest(terminal_id="term-1", command_line="npm build")
        res = await terminal_execute_command(req, ctx)

        assert res.success is False
        assert res.execution_status == "failed"
        assert res.output["accepted"] is False
        assert "command_id" in res.output
        assert res.output["command_id"].startswith("cmd_err_")
        assert res.output["terminal_id"] == "term-1"
        mock_recorder.async_record_terminal_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_execute_command_generic_exception_returns_command_id() -> None:
    mock_recorder = MagicMock()
    mock_recorder.async_record_terminal_command = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    with patch("agent_runtime.terminal.terminal_factory.TerminalFactory.create") as mock_factory:
        mock_gateway = MagicMock()
        mock_factory.return_value = mock_gateway
        mock_gateway.execute_command = AsyncMock(side_effect=ValueError("终端当前不可执行新命令"))

        ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
        req = TerminalExecuteCommandToolRequest(terminal_id="term-1", command_line="npm run dev")
        res = await terminal_execute_command(req, ctx)

        assert res.success is False
        assert res.execution_status == "failed"
        assert res.output["accepted"] is False
        assert "command_id" in res.output
        assert res.output["command_id"].startswith("cmd_err_")
        assert res.output["terminal_id"] == "term-1"
        assert "终端当前不可执行新命令" in res.output["error"]
        mock_recorder.async_record_terminal_command.assert_awaited_once()


# ── File Tools Hooks: task_artifact_records ───────────────────────────────

def test_file_modify_hook(tmp_path: Any) -> None:
    from agent_runtime.file_tools.file_edit_tools import FileModifyEdit, FileModifyRequest, file_modify

    mock_recorder = MagicMock()
    set_task_note_event_recorder(mock_recorder)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    target_file = project_root / "hello.py"
    target_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
    req = FileModifyRequest(
        project_root=str(project_root),
        file_path="hello.py",
        edits=[
            FileModifyEdit(
                feature_desc="change return value",
                search_lines=["    return 1"],
                replace_lines=["    return 42"],
            )
        ],
    )
    res = file_modify(req, ctx)

    assert res.success is True
    assert target_file.read_text(encoding="utf-8") == "def foo():\n    return 42\n"
    mock_recorder.record_file_artifact.assert_called_once()
    kwargs = mock_recorder.record_file_artifact.call_args.kwargs
    assert kwargs["task_id"] == "task-1"
    assert kwargs["agent_instance_id"] == "inst-1"
    assert kwargs["op_type"] == "file_modify"
    assert kwargs["file_path"] == str(target_file.resolve())
    assert len(kwargs["edits"]) == 1
    assert kwargs["edits"][0]["feature_desc"] == "change return value"
    assert kwargs["edits"][0]["search_lines"] == ["    return 1"]
    assert kwargs["edits"][0]["replace_lines"] == ["    return 42"]


def test_file_modify_dry_run_and_failure_do_not_record(tmp_path: Any) -> None:
    from agent_runtime.file_tools.file_edit_tools import FileModifyEdit, FileModifyRequest, file_modify

    mock_recorder = MagicMock()
    set_task_note_event_recorder(mock_recorder)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    target_file = project_root / "hello.py"
    target_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")

    # 1. dry_run should NOT record
    req_dry = FileModifyRequest(
        project_root=str(project_root),
        file_path="hello.py",
        dry_run=True,
        edits=[
            FileModifyEdit(
                feature_desc="change return value",
                search_lines=["    return 1"],
                replace_lines=["    return 42"],
            )
        ],
    )
    res_dry = file_modify(req_dry, ctx)
    assert res_dry.success is True
    mock_recorder.record_file_artifact.assert_not_called()

    # 2. search block not found failure should NOT record
    req_fail = FileModifyRequest(
        project_root=str(project_root),
        file_path="hello.py",
        edits=[
            FileModifyEdit(
                feature_desc="non-existent search",
                search_lines=["    non_existent()"],
                replace_lines=["    return 42"],
            )
        ],
    )
    res_fail = file_modify(req_fail, ctx)
    assert res_fail.success is False
    mock_recorder.record_file_artifact.assert_not_called()


def test_file_create_and_delete_hooks(tmp_path: Any) -> None:
    from agent_runtime.file_tools.file_edit_tools import (
        FileCreateRequest,
        FileDeleteRequest,
        file_create,
        file_delete,
    )

    mock_recorder = MagicMock()
    set_task_note_event_recorder(mock_recorder)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")

    # 1. file_create
    req_create = FileCreateRequest(
        project_root=str(project_root),
        file_path="new_file.txt",
        content_lines=["line 1", "line 2"],
    )
    res_create = file_create(req_create, ctx)
    assert res_create.success is True
    mock_recorder.record_file_artifact.assert_called_once()
    kwargs = mock_recorder.record_file_artifact.call_args.kwargs
    assert kwargs["op_type"] == "file_create"
    assert kwargs["content_lines"] == ["line 1", "line 2"]

    mock_recorder.reset_mock()

    # 2. file_delete
    req_del = FileDeleteRequest(
        project_root=str(project_root),
        file_path="new_file.txt",
        reason="clean up temp file",
    )
    res_del = file_delete(req_del, ctx)
    assert res_del.success is True
    mock_recorder.record_file_artifact.assert_called_once()
    kwargs_del = mock_recorder.record_file_artifact.call_args.kwargs
    assert kwargs_del["op_type"] == "file_delete"
    assert kwargs_del["content"] == "clean up temp file"


def test_file_move_hook(tmp_path: Any) -> None:
    from agent_runtime.file_tools.file_edit_tools import FileMoveRequest, file_move

    mock_recorder = MagicMock()
    set_task_note_event_recorder(mock_recorder)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    src_file = project_root / "src.txt"
    src_file.write_text("data", encoding="utf-8")

    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")
    req_move = FileMoveRequest(
        project_root=str(project_root),
        source_path="src.txt",
        target_path="dst.txt",
        reason="rename to dst",
    )
    res_move = file_move(req_move, ctx)
    assert res_move.success is True
    mock_recorder.record_file_artifact.assert_called_once()
    kwargs = mock_recorder.record_file_artifact.call_args.kwargs
    assert kwargs["op_type"] == "file_move"
    assert kwargs["source_path"] == str(src_file.resolve())
    assert kwargs["target_path"] == str((project_root / "dst.txt").resolve())
    assert kwargs["content"] == "rename to dst"


# ── Subagent Tool Hooks: relationships & messages ─────────────────────────

@pytest.mark.asyncio
async def test_subagent_tool_hooks() -> None:
    from agent_runtime.subagent.subagent_tool import (
        CreateSubagentRequest,
        SendAgentMessageRequest,
        create_subagent,
        send_agent_message,
    )

    mock_recorder = MagicMock()
    mock_recorder.async_record_subagent_relationship = AsyncMock(return_value=True)
    mock_recorder.async_record_agent_message = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-parent")

    with patch("agent_runtime.subagent.subagent_tool.get_subagent_runtime") as mock_get_rt:
        mock_rt = MagicMock()
        mock_get_rt.return_value = mock_rt
        mock_rt.create_subagent = AsyncMock(
            return_value={
                "status": "created",
                "task_id": "task-1",
                "child_agent_instance_id": "inst-child-001",
            }
        )
        mock_rt.send_agent_message = AsyncMock(return_value={"status": "sent"})

        # 1. create_subagent with initial_message
        req_create = CreateSubagentRequest(
            agent_definition_id="def-worker",
            initial_message="Start working on module A",
        )
        res_create = await create_subagent(req_create, ctx)
        assert res_create.success is True
        mock_recorder.async_record_subagent_relationship.assert_awaited_once_with(
            task_id="task-1",
            parent_instance_id="inst-parent",
            child_instance_id="inst-child-001",
            delegation_content="Start working on module A",
            fork_message_id=None,
        )
        mock_recorder.async_record_agent_message.assert_awaited_once_with(
            task_id="task-1",
            from_instance_id="inst-parent",
            to_instance_id="inst-child-001",
            target_agent_type="child",
            message="Start working on module A",
        )

        mock_recorder.reset_mock()

        # 2. send_agent_message
        mock_instance = MagicMock()
        mock_instance.task_id = "task-1"
        mock_task = MagicMock()
        mock_relation = MagicMock()
        mock_relation.parent_agent_instance_id = "inst-parent"
        mock_task.child_relation_of.return_value = mock_relation
        mock_rt._runtime_gateway.get_agent_instance.return_value = mock_instance
        mock_rt._runtime_gateway.get_task.return_value = mock_task

        req_msg = SendAgentMessageRequest(
            to_agent_instance_id="inst-child-001",
            content="Check the status",
        )
        res_msg = await send_agent_message(req_msg, ctx)
        assert res_msg.success is True
        mock_recorder.async_record_agent_message.assert_awaited_once_with(
            task_id="task-1",
            from_instance_id="inst-parent",
            to_instance_id="inst-child-001",
            target_agent_type="child",
            message="Check the status",
        )


# ── LACP Bridge Hook: remote agent message ────────────────────────────────

@pytest.mark.asyncio
async def test_lacp_bridge_inbound_hook() -> None:
    from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge
    from agent_runtime.lacp.models import LacpInboundMessage

    mock_recorder = MagicMock()
    mock_recorder.async_record_remote_agent_message = AsyncMock(return_value=True)
    set_task_note_event_recorder(mock_recorder)

    mock_lacp_client = MagicMock()
    mock_registry = MagicMock()
    mock_registry.is_registered.return_value = True
    mock_runtime_gateway = MagicMock()
    mock_instance = MagicMock()
    mock_instance.task_id = "task-lacp-1"
    mock_runtime_gateway.get_agent_instance.return_value = mock_instance
    mock_agent_def_gateway = MagicMock()
    mock_engine = MagicMock()
    mock_engine.submit_agent_instance_message = AsyncMock()

    bridge = AgentRuntimeLacpBridge(
        lacp_client=mock_lacp_client,
        registration_registry=mock_registry,
        runtime_gateway=mock_runtime_gateway,
        agent_definition_gateway=mock_agent_def_gateway,
        execution_engine=mock_engine,
    )

    inbound = LacpInboundMessage(
        message_id="msg-inbound-1",
        source_tag="@RemoteArchitect",
        to_instance_id="inst-local-01",
        from_instance_id="inst-remote-02",
        from_role_name="RemoteArchitect",
        content="Please verify the integration test.",
    )

    handled = await bridge.handle_inbound_message(inbound)
    assert handled is True
    mock_recorder.async_record_remote_agent_message.assert_awaited_once_with(
        task_id="task-lacp-1",
        agent_instance_id="inst-local-01",
        remote_agent_instance_id="inst-remote-02",
        message_type="receive",
        message="Please verify the integration test.",
    )


def test_recorder_failure_count_when_pg_disconnected_file_create_succeeds(tmp_path: Any) -> None:
    """验收 4: 断开 PG 时 file_create 仍成功，recorder 失败计数 +1。"""
    from agent_runtime.task_note.task_note_event_recorder import TaskNoteEventRecorder
    from agent_runtime.file_tools.file_edit_tools import FileCreateRequest, file_create

    # 真实的 recorder，但是没有 connection_factory（模拟断开 PG）
    real_recorder = TaskNoteEventRecorder(connection_factory=None)
    assert real_recorder.failure_count == 0
    set_task_note_event_recorder(real_recorder)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    ctx = ToolRuntimeContext(task_id="task-1", agent_instance_id="inst-1")

    req_create = FileCreateRequest(
        project_root=str(project_root),
        file_path="hello.txt",
        content_lines=["hello world"],
    )
    # file_create 应该依然成功
    res = file_create(req_create, ctx)
    assert res.success is True
    assert (project_root / "hello.txt").exists()

    # recorder 失败计数 +1
    assert real_recorder.failure_count == 1


