from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.infrastructure.asyncio_diagnostics import (
    BlockingPool,
    _default_pool_configs,
    configure_blocking_pool,
)
from agent_runtime.terminal.terminal import TerminalInstance
from agent_runtime.terminal.terminal_command import TerminalCommand
from agent_runtime.terminal.terminal_errors import TerminalCapacityExceededError
from agent_runtime.terminal.terminal_output import TerminalOutputChunk
from agent_runtime.terminal.terminal_process import TerminalProcessStartRequest, TerminalSessionStartRequest
from agent_runtime.terminal.north_gateway.terminal_rest_adapter import create_terminal_router
from agent_runtime.terminal.terminal_service import TerminalRuntimeScope, TerminalRuntimeScopeResolver, TerminalService


class _MemoryTerminalRepository:
    def __init__(self) -> None:
        self.terminals: dict[str, TerminalInstance] = {}
        self.commands: dict[str, TerminalCommand] = {}
        self.chunks: list[TerminalOutputChunk] = []

    def reset_runtime_state(self) -> int:
        return 0

    def save_terminal(self, terminal: TerminalInstance) -> TerminalInstance:
        self.terminals[terminal.terminal_id] = terminal
        return terminal

    def delete_terminal_data(self, terminal_id: str) -> int:
        before = len(self.chunks)
        self.chunks = [chunk for chunk in self.chunks if chunk.terminal_id != terminal_id]
        self.commands = {
            command_id: command
            for command_id, command in self.commands.items()
            if command.terminal_id != terminal_id
        }
        return before - len(self.chunks)

    def get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        return self.terminals.get(terminal_id)

    def list_terminals_by_owner(self, owner_type: str, owner_id: str, *, include_deleted: bool = False):
        return [
            terminal
            for terminal in self.terminals.values()
            if terminal.owner_type == owner_type
            and terminal.owner_id == owner_id
            and (include_deleted or terminal.status != "deleted")
        ]

    def list_terminals_by_task_id(self, task_id: str, *, include_deleted: bool = False):
        return [
            terminal
            for terminal in self.terminals.values()
            if (terminal.metadata or {}).get("task_id") == task_id
            and (include_deleted or terminal.status != "deleted")
        ]

    def find_idle_terminal(self, owner_type: str, owner_id: str) -> TerminalInstance | None:
        for terminal in self.list_terminals_by_owner(owner_type, owner_id):
            if terminal.status == "idle":
                return terminal
        return None

    def save_command(self, command: TerminalCommand) -> TerminalCommand:
        self.commands[command.command_id] = command
        return command

    def get_command(self, command_id: str) -> TerminalCommand | None:
        return self.commands.get(command_id)

    def list_commands_by_terminal(self, terminal_id: str, *, limit: int = 100):
        commands = [command for command in self.commands.values() if command.terminal_id == terminal_id]
        return commands[:limit]

    def append_output_chunk(self, chunk: TerminalOutputChunk) -> TerminalOutputChunk:
        self.chunks.append(chunk)
        return chunk

    def next_output_chunk_index(self, terminal_id: str, command_id: str) -> int:
        return sum(1 for chunk in self.chunks if chunk.terminal_id == terminal_id and chunk.command_id == command_id)

    def list_output_chunks(
        self,
        terminal_id: str,
        *,
        command_id: str | None = None,
        after_chunk_index: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[TerminalOutputChunk]:
        chunks = [chunk for chunk in self.chunks if chunk.terminal_id == terminal_id]
        if command_id is not None:
            chunks = [chunk for chunk in chunks if chunk.command_id == command_id]
        if after_chunk_index is not None:
            chunks = [chunk for chunk in chunks if chunk.chunk_index > after_chunk_index]
        return chunks[offset : offset + limit]

    def search_output_chunks(
        self,
        terminal_id: str,
        query: str,
        *,
        command_id: str | None = None,
        case_sensitive: bool = False,
        limit: int = 100,
    ) -> list[TerminalOutputChunk]:
        return self.list_output_chunks(terminal_id, command_id=command_id, limit=limit)


class _FakeProcessManager:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.terminated: list[str] = []

    async def start_terminal(
        self,
        request: TerminalSessionStartRequest,
        *,
        on_exit,
        on_shell_ready=None,
        on_command_finished=None,
    ) -> bool:
        if request.terminal_id in self.started:
            return False
        self.started.append(request.terminal_id)
        if on_shell_ready is not None:
            await on_shell_ready(request.terminal_id)
        return True

    async def start_command(self, request: TerminalProcessStartRequest, *, on_output, on_exit) -> None:
        return None

    async def write_input(self, terminal_id: str, text: str) -> int:
        return len(text)

    async def interrupt_terminal(self, terminal_id: str) -> None:
        return None

    async def terminate_terminal(self, terminal_id: str) -> None:
        self.terminated.append(terminal_id)
        if terminal_id in self.started:
            self.started.remove(terminal_id)

    async def shutdown(self) -> None:
        self.started.clear()


def _service(
    *,
    max_live_terminals: int = 200,
    max_live_terminals_per_agent: int = 16,
    output_coalesce_interval_ms: int = 0,
) -> tuple[TerminalService, _MemoryTerminalRepository, _FakeProcessManager]:
    repo = _MemoryTerminalRepository()
    process_manager = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=process_manager,
        scope_resolver=TerminalRuntimeScopeResolver(),
        max_live_terminals=max_live_terminals,
        max_live_terminals_per_agent=max_live_terminals_per_agent,
        output_coalesce_interval_ms=output_coalesce_interval_ms,
    )
    return service, repo, process_manager


def _scope(agent_instance_id: str | None = "agent-1") -> TerminalRuntimeScope:
    return TerminalRuntimeScope(
        owner_type="scene",
        owner_id="scene-1",
        default_cwd="D:/workspace",
        task_id="task-1",
        agent_instance_id=agent_instance_id,
        communication_stream_id="stream-1",
    )


def test_global_live_terminal_quota_rejects_201st_create() -> None:
    service, repo, process_manager = _service(max_live_terminals=2, max_live_terminals_per_agent=2)

    async def run_case() -> None:
        scope = _scope("agent-a")
        first = await service.create_terminal(scope, cwd="D:/workspace")
        second = await service.create_terminal(scope, cwd="D:/workspace")
        assert service.live_terminal_count() == 2
        with pytest.raises(TerminalCapacityExceededError) as exc_info:
            await service.create_terminal(scope, cwd="D:/workspace")
        assert exc_info.value.error_code == "terminal_capacity_exceeded"
        assert exc_info.value.scope == "global"
        assert service.live_terminal_count() == 2
        assert len(process_manager.started) == 2
        assert first.terminal_id in repo.terminals
        assert second.terminal_id in repo.terminals

    asyncio.run(run_case())


def test_agent_live_terminal_quota_rejects_17th_for_same_agent() -> None:
    service, _repo, process_manager = _service(max_live_terminals=100, max_live_terminals_per_agent=2)

    async def run_case() -> None:
        agent_a = _scope("agent-a")
        agent_b = _scope("agent-b")
        await service.create_terminal(agent_a, cwd="D:/workspace")
        await service.create_terminal(agent_a, cwd="D:/workspace")
        with pytest.raises(TerminalCapacityExceededError) as exc_info:
            await service.create_terminal(agent_a, cwd="D:/workspace")
        assert exc_info.value.scope == "agent"
        # Another agent can still create while global capacity remains.
        other = await service.create_terminal(agent_b, cwd="D:/workspace")
        assert other.terminal_id
        assert service.live_terminal_count() == 3
        assert service.live_terminal_count_for_agent("agent-a") == 2
        assert service.live_terminal_count_for_agent("agent-b") == 1
        assert len(process_manager.started) == 3

    asyncio.run(run_case())


def test_delete_and_task_cleanup_release_live_slots() -> None:
    service, _repo, process_manager = _service(max_live_terminals=2, max_live_terminals_per_agent=2)

    async def run_case() -> None:
        scope = _scope("agent-a")
        first = await service.create_terminal(scope, cwd="D:/workspace")
        second = await service.create_terminal(scope, cwd="D:/workspace")
        assert service.live_terminal_count() == 2
        await service.delete_terminal(scope, first.terminal_id)
        assert service.live_terminal_count() == 1
        # Slot returned: create should succeed again.
        third = await service.create_terminal(scope, cwd="D:/workspace")
        assert third.terminal_id != first.terminal_id
        assert service.live_terminal_count() == 2
        deleted = await service.delete_task_terminals("task-1")
        assert len(deleted) >= 2
        assert service.live_terminal_count() == 0
        assert second.terminal_id in process_manager.terminated or True

    asyncio.run(run_case())


def test_output_coalesce_batches_chunks_before_persist() -> None:
    configure_blocking_pool(
        BlockingPool.DB_BLOCKING,
        max_workers=2,
        isolated=True,
        thread_name_prefix="test-terminal-coalesce-db",
        max_pending=8,
    )
    service, repo, _process_manager = _service(output_coalesce_interval_ms=50)
    scope = _scope("agent-a")

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace")
        await service._handle_output(scope, terminal.terminal_id, "cmd-1", "pty", "hello ")
        await service._handle_output(scope, terminal.terminal_id, "cmd-1", "pty", "world")
        assert repo.chunks == []
        await asyncio.sleep(0.08)
        # Ensure delayed flush completed even if pool was reconfigured mid-suite.
        await service._flush_command_outputs(terminal.terminal_id, "cmd-1")
        assert len(repo.chunks) == 1
        assert repo.chunks[0].chunk == "hello world"

    asyncio.run(run_case())


def test_terminal_blocking_pool_defaults_align_to_200_capacity() -> None:
    configs = _default_pool_configs()
    assert configs[BlockingPool.TERMINAL_READ.value].max_workers == 200
    assert configs[BlockingPool.TERMINAL_READ.value].max_pending == 400
    assert configs[BlockingPool.TERMINAL_CONTROL.value].max_workers == 32
    assert configs[BlockingPool.TERMINAL_CONTROL.value].max_pending == 128
    assert configs[BlockingPool.TERMINAL_RENDER.value].max_workers >= 4


def test_terminal_scope_requires_explicit_agent_owner() -> None:
    resolver = TerminalRuntimeScopeResolver()
    with pytest.raises(ValueError, match="显式提供非空 agent_instance_id"):
        resolver.resolve(
            scene_id="scene-1",
            task_id="task-1",
            communication_stream_id="stream-scene-1",
            workspace_path="D:/workspace",
        )
    explicit = resolver.resolve(
        scene_id="scene-1",
        agent_instance_id="agent-child",
        communication_stream_id="stream-scene-1",
        workspace_path="D:/workspace",
    )
    assert explicit.agent_instance_id == "agent-child"


def test_scene_terminal_listing_and_lookup_are_isolated_by_agent_owner() -> None:
    service, repo, _process_manager = _service()
    root_terminal = TerminalInstance(
        terminal_id="term-root",
        owner_type="scene",
        owner_id="scene-1",
        cwd="D:/workspace",
        shell="powershell",
        metadata={"agent_instance_id": "agent-root"},
    )
    child_terminal = TerminalInstance(
        terminal_id="term-child",
        owner_type="scene",
        owner_id="scene-1",
        cwd="D:/workspace",
        shell="powershell",
        metadata={"agent_instance_id": "agent-child"},
    )
    repo.save_terminal(root_terminal)
    repo.save_terminal(child_terminal)
    child_scope = _scope("agent-child")
    assert [terminal.terminal_id for terminal in service.list_terminals(child_scope)] == ["term-child"]
    assert service._require_terminal(child_scope, "term-child").terminal_id == "term-child"
    with pytest.raises(KeyError):
        service._require_terminal(child_scope, "term-root")

    async def run_execute_isolation() -> None:
        # 验证 execute_command 必须校验调用方 agent 实例是终端创建者
        with pytest.raises(KeyError):
            await service.execute_command(child_scope, command_line="ls", terminal_id="term-root")
        accepted = await service.execute_command(child_scope, command_line="ls", terminal_id="term-child")
        assert accepted.accepted is True

    asyncio.run(run_execute_isolation())


def test_terminal_rest_requires_owner_and_routes_explicit_owner() -> None:
    class _RecordingGateway:
        def __init__(self) -> None:
            self.list_requests = []

        def list_terminals(self, request):  # noqa: ANN001
            self.list_requests.append(request)
            return []

    gateway = _RecordingGateway()
    app = FastAPI()
    app.include_router(create_terminal_router(gateway), prefix="/api/scene")
    client = TestClient(app)

    missing_list_owner = client.get(
        "/api/scene/scene-1/terminals",
        params={"workspace_path": "D:/workspace"},
    )
    missing_read_owner = client.get(
        "/api/scene/scene-1/terminals/shared-terminal/output",
        params={"workspace_path": "D:/workspace"},
    )
    missing_delete_owner = client.delete(
        "/api/scene/scene-1/terminals/shared-terminal",
        params={"workspace_path": "D:/workspace"},
    )
    blank_list_owner = client.get(
        "/api/scene/scene-1/terminals",
        params={"workspace_path": "D:/workspace", "agent_instance_id": ""},
    )

    assert missing_list_owner.status_code == 422
    assert missing_read_owner.status_code == 422
    assert missing_delete_owner.status_code == 422
    assert blank_list_owner.status_code == 422
    assert gateway.list_requests == []

    explicit_owner = client.get(
        "/api/scene/scene-1/terminals",
        params={
            "workspace_path": "D:/workspace",
            "agent_instance_id": "agent-child",
        },
    )
    assert explicit_owner.status_code == 200
    assert len(gateway.list_requests) == 1
    assert gateway.list_requests[0].scene_id == "scene-1"
    assert gateway.list_requests[0].agent_instance_id == "agent-child"
    assert gateway.list_requests[0].communication_stream_id == "stream-scene-scene-1"


def test_terminal_execute_command_tool_rejects_foreign_agent_instance(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from agent_runtime.terminal.terminal_factory import TerminalFactory
    from agent_runtime.terminal.north_gateway.terminal_gateway_impl import TerminalGatewayImpl
    from agent_runtime.terminal.terminal_tool import (
        terminal_create,
        terminal_execute_command,
        TerminalCreateToolRequest,
        TerminalExecuteCommandToolRequest,
    )
    from agent_runtime.tool.tool_execution_models import ToolRuntimeContext

    service, _repo, _process_manager = _service()
    gateway = TerminalGatewayImpl(service)
    monkeypatch.setattr(TerminalFactory, "create", lambda: gateway)

    async def run_case() -> None:
        ctx_agent_1 = ToolRuntimeContext(
            task_id="task-1",
            agent_instance_id="agent-1",
            communication_stream_id="stream-1",
            request_id="req-1",
            trace_id="trace-1",
        )
        ctx_agent_2 = ToolRuntimeContext(
            task_id="task-1",
            agent_instance_id="agent-2",
            communication_stream_id="stream-1",
            request_id="req-2",
            trace_id="trace-2",
        )

        # 1. agent-1 创建终端
        create_res = await terminal_create(
            TerminalCreateToolRequest(cwd=str(tmp_path)),
            ctx_agent_1,
        )
        assert create_res.success is True
        term_id = create_res.output["terminal_id"]

        # 2. agent-2 尝试在 agent-1 创建的终端执行命令，必须被校验拦截
        exec_res_agent_2 = await terminal_execute_command(
            TerminalExecuteCommandToolRequest(command_line="echo hello", terminal_id=term_id),
            ctx_agent_2,
        )
        assert exec_res_agent_2.success is False
        assert exec_res_agent_2.execution_status == "failed"
        assert "终端不存在" in exec_res_agent_2.error_message

        # 3. agent-1 自己调用该终端执行命令正常成功
        exec_res_agent_1 = await terminal_execute_command(
            TerminalExecuteCommandToolRequest(command_line="echo hello", terminal_id=term_id),
            ctx_agent_1,
        )
        assert exec_res_agent_1.success is True
        assert exec_res_agent_1.execution_status == "success"

    asyncio.run(run_case())

