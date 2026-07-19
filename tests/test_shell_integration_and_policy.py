from __future__ import annotations

import asyncio

import pytest

from agent_runtime.terminal.shell_integration import (
    OrderedSaiStreamParser,
    StreamToken,
    TokenKind,
    build_bash_integration_script,
    build_powershell_integration_script,
    build_zsh_integration_script,
)
from agent_runtime.terminal.shell_policy import (
    platform_supported_shell,
    require_shell_executable,
    resolve_supported_shell,
)
from agent_runtime.terminal.terminal import TerminalInstance
from agent_runtime.terminal.terminal_command import (
    TERMINAL_COMMAND_STATUS_COMPLETED,
    TERMINAL_COMMAND_STATUS_RUNNING,
    TerminalCommand,
)
from agent_runtime.terminal.terminal_output import TerminalOutputChunk
from agent_runtime.terminal.terminal_process import TerminalProcessStartRequest, TerminalSessionStartRequest
from agent_runtime.terminal.terminal_service import TerminalRuntimeScope, TerminalRuntimeScopeResolver, TerminalService


def test_resolve_supported_shell_rejects_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.sys.platform", "win32")
    assert resolve_supported_shell(None) == "powershell"
    assert resolve_supported_shell("powershell.exe") == "powershell"
    with pytest.raises(ValueError):
        resolve_supported_shell("pwsh")
    with pytest.raises(ValueError):
        resolve_supported_shell("cmd")
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.sys.platform", "darwin")
    assert resolve_supported_shell("zsh") == "zsh"
    with pytest.raises(ValueError):
        resolve_supported_shell("bash")
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.sys.platform", "linux")
    assert resolve_supported_shell("bash") == "bash"
    with pytest.raises(ValueError):
        resolve_supported_shell("zsh")
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.sys.platform", "freebsd")
    with pytest.raises(RuntimeError):
        platform_supported_shell()


def test_require_windows_powershell_uses_system_executable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "powershell.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.sys.platform", "win32")
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.os.path.expandvars", lambda _: str(executable))
    monkeypatch.setattr("agent_runtime.terminal.shell_policy.shutil.which", lambda _: None)
    assert require_shell_executable("powershell.exe") == str(executable)


def test_powershell_launch_uses_managed_51_arguments(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    script_path = tmp_path / "managed.ps1"
    script_path.write_text("# managed", encoding="utf-8")
    executable = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(pty, "resolve_supported_shell", lambda _: "powershell")
    monkeypatch.setattr(pty, "require_shell_executable", lambda _: executable)
    monkeypatch.setattr(pty, "integration_script_for_shell", lambda _: "# managed")
    monkeypatch.setattr(pty, "_write_temp_script", lambda *_args, **_kwargs: str(script_path))
    argv, _env, temp_paths = pty._build_shell_launch("powershell")
    assert argv == [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    assert temp_paths == [str(script_path)]


def test_ordered_parser_preserves_visible_done_ready_order() -> None:
    parser = OrderedSaiStreamParser()
    tokens = parser.feed("out1\x1b]633;SAI;D;0\x07prompt\x1b]633;SAI;B\x07")
    kinds = [t.kind for t in tokens]
    assert kinds == [TokenKind.VISIBLE, TokenKind.DONE, TokenKind.VISIBLE, TokenKind.READY]
    assert tokens[0].text == "out1"
    assert tokens[1].exit_code == 0 and tokens[1].exit_code_present
    assert tokens[2].text == "prompt"


def test_ordered_parser_handles_split_frames_and_st_terminator() -> None:
    parser = OrderedSaiStreamParser()
    t1 = parser.feed("a\x1b]633;SAI;D;1")
    t2 = parser.feed("\x1b\\b\x1b]633;SAI;B\x07")
    tokens = t1 + t2
    assert [t.kind for t in tokens] == [TokenKind.VISIBLE, TokenKind.DONE, TokenKind.VISIBLE, TokenKind.READY]
    assert tokens[1].exit_code == 1


def test_shell_scripts_contain_done_and_ready_only() -> None:
    powershell = build_powershell_integration_script()
    bash = build_bash_integration_script()
    zsh = build_zsh_integration_script()
    for script in (powershell, bash, zsh):
        assert "SAI;D" in script or 'D;' in script or '"D;"' in script
        assert "SAI;B" in script or "'B'" in script
        assert "SAI;C" not in script
        assert "nonce" not in script.lower()
    # The exact Windows PowerShell 5.1 gate must hard-exit before Prompt/READY hooks are installed.
    gate_idx = powershell.find("PSVersionTable.PSVersion.Major -ne 5")
    minor_gate_idx = powershell.find("PSVersionTable.PSVersion.Minor -ne 1")
    prompt_idx = powershell.find("function Global:Prompt")
    exit_idx = powershell.find("exit 1")
    assert 0 <= gate_idx < minor_gate_idx < exit_idx < prompt_idx
    assert "PowerShell 7" not in powershell


class _MemoryRepo:
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
        return 0

    def get_terminal(self, terminal_id: str) -> TerminalInstance | None:
        return self.terminals.get(terminal_id)

    def list_terminals_by_owner(self, owner_type: str, owner_id: str, *, include_deleted: bool = False):
        return [t for t in self.terminals.values() if t.owner_type == owner_type and t.owner_id == owner_id]

    def list_terminals_by_task_id(self, task_id: str, *, include_deleted: bool = False):
        return []

    def find_idle_terminal(self, owner_type: str, owner_id: str) -> TerminalInstance | None:
        for t in self.list_terminals_by_owner(owner_type, owner_id):
            if t.status == "idle":
                return t
        return None

    def save_command(self, command: TerminalCommand) -> TerminalCommand:
        self.commands[command.command_id] = command
        return command

    def get_command(self, command_id: str) -> TerminalCommand | None:
        return self.commands.get(command_id)

    def list_commands_by_terminal(self, terminal_id: str, *, limit: int = 100):
        return [c for c in self.commands.values() if c.terminal_id == terminal_id][:limit]

    def append_output_chunk(self, chunk: TerminalOutputChunk) -> TerminalOutputChunk:
        self.chunks.append(chunk)
        return chunk

    def next_output_chunk_index(self, terminal_id: str, command_id: str) -> int:
        return len([c for c in self.chunks if c.terminal_id == terminal_id and c.command_id == command_id])

    def list_output_chunks(self, terminal_id: str, *, command_id=None, after_chunk_index=None, offset=0, limit=500):
        chunks = [c for c in self.chunks if c.terminal_id == terminal_id]
        if command_id is not None:
            chunks = [c for c in chunks if c.command_id == command_id]
        if after_chunk_index is not None:
            chunks = [c for c in chunks if c.chunk_index > after_chunk_index]
        return chunks[offset: offset + limit]

    def search_output_chunks(self, terminal_id: str, query: str, **kwargs):
        return self.list_output_chunks(terminal_id, command_id=kwargs.get("command_id"), limit=kwargs.get("limit", 100))


class _FakeProcessManager:
    def __init__(self) -> None:
        self.finished_cb = None
        self.ready = None

    async def start_terminal(self, request: TerminalSessionStartRequest, *, on_exit, on_shell_ready=None, on_command_finished=None):
        self.ready = on_shell_ready
        self.finished_cb = on_command_finished
        if on_shell_ready:
            await on_shell_ready(request.terminal_id)
        return True

    async def start_command(self, request: TerminalProcessStartRequest, *, on_output, on_exit) -> None:
        await on_output("pty", "hello\n")

    async def write_input(self, terminal_id: str, text: str) -> int:
        return len(text)

    async def interrupt_terminal(self, terminal_id: str) -> None:
        return None

    async def terminate_terminal(self, terminal_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_wait_early_return_and_busy_until_ready() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo hello", terminal_id=terminal.terminal_id)
        assert accepted.command.status == TERMINAL_COMMAND_STATUS_RUNNING
        assert repo.get_terminal(terminal.terminal_id).status == "busy"

        async def finish_later():
            await asyncio.sleep(0.05)
            await pm.finished_cb(accepted.command.command_id, 0, None, "completed")

        task = asyncio.create_task(finish_later())
        started = asyncio.get_running_loop().time()
        await service._wait_for_read(
            terminal_id=terminal.terminal_id,
            command_id=accepted.command.command_id,
            mode="tail",
            after_chunk_index=None,
            wait_timeout_ms=5000,
        )
        wait_elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
        await task
        # Deterministic: Event wake is independent of render pool cold start.
        assert wait_elapsed_ms < 500
        result = await service.read_output(
            scope,
            terminal_id=terminal.terminal_id,
            command_id=accepted.command.command_id,
            mode="tail",
            wait_timeout_ms=0,
        )
        assert result.command_finished is True
        assert result.command_status == TERMINAL_COMMAND_STATUS_COMPLETED
        assert repo.get_terminal(terminal.terminal_id).status == "busy"
        await pm.ready(terminal.terminal_id)
        assert repo.get_terminal(terminal.terminal_id).status == "idle"

    asyncio.run(run_case())


def test_unix_start_terminal_binds_shell_ready_and_command_finished_callbacks() -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    manager = pty.PtyTerminalProcessManager()
    captured: dict[str, object] = {}

    async def fake_start_unix(request, *, on_exit):
        session = pty._TerminalSession(
            terminal_id=request.terminal_id,
            shell="bash",
            on_exit=on_exit,
            runtime_token=request.runtime_token,
        )
        captured["session"] = session
        return session

    async def run_case() -> None:
        async def on_shell_ready(terminal_id: str) -> None:
            captured["ready"] = terminal_id

        async def on_command_finished(command_id, exit_code, error_message, final_status) -> None:
            captured["finished"] = (command_id, exit_code, error_message, final_status)

        session = await fake_start_unix(
            TerminalSessionStartRequest(terminal_id="term-u", cwd="/", shell="bash", runtime_token="rt"),
            on_exit=lambda *_: None,
        )
        # Simulate the production assignment path used by start_terminal.
        session.on_shell_ready = on_shell_ready
        session.on_command_finished = on_command_finished
        manager._sessions[session.terminal_id] = session
        session.shell_ready = True
        session.shell_ready_event.set()

        finished_seen: list[tuple] = []
        ready_seen: list[str] = []

        async def on_finished(command_id, exit_code, error_message, final_status):
            finished_seen.append((command_id, exit_code, error_message, final_status))

        async def on_ready(terminal_id: str):
            ready_seen.append(terminal_id)

        session.on_command_finished = on_finished
        session.on_shell_ready = on_ready
        active = pty._ActiveCommand(
            command_id="cmd-u",
            on_output=lambda *_: None,
            on_exit=lambda *_: None,
        )
        session.active_command = active
        session.shell_ready = False
        await manager._handle_stream_token(
            session,
            StreamToken(kind=TokenKind.DONE, exit_code=0, exit_code_present=True),
        )
        assert finished_seen == [("cmd-u", 0, None, "completed")]
        assert session.awaiting_command is not None
        await manager._handle_stream_token(session, StreamToken(kind=TokenKind.READY))
        assert ready_seen == ["term-u"]
        assert session.shell_ready is True
        assert session.awaiting_command is None

    asyncio.run(run_case())


def test_start_terminal_assigns_callbacks_on_unix_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    manager = pty.PtyTerminalProcessManager()

    async def fake_start_unix(request, *, on_exit):
        session = pty._TerminalSession(
            terminal_id=request.terminal_id,
            shell="bash",
            on_exit=on_exit,
            runtime_token=request.runtime_token,
        )
        # Already READY so start_terminal's wait_for returns immediately.
        session.shell_ready = True
        session.shell_ready_event.set()
        return session

    class _FakeTask:
        def get_name(self):
            return "fake"

    def fake_create_task(coro, **kwargs):
        try:
            coro.close()
        except Exception:
            pass
        return _FakeTask()

    monkeypatch.setattr(pty.sys, "platform", "linux")
    monkeypatch.setattr(pty, "_start_unix_terminal", fake_start_unix)
    monkeypatch.setattr(pty.get_runtime_task_registry(), "create_task", fake_create_task)

    async def run_case() -> None:
        async def on_ready(terminal_id: str) -> None:
            return None

        async def on_finished(command_id, exit_code, error_message, final_status) -> None:
            return None

        started = await manager.start_terminal(
            TerminalSessionStartRequest(terminal_id="term-bind", cwd="/", shell="bash", runtime_token="rt"),
            on_exit=lambda *_: None,
            on_shell_ready=on_ready,
            on_command_finished=on_finished,
        )
        assert started is True
        session = manager._sessions["term-bind"]
        assert session.on_shell_ready is on_ready
        assert session.on_command_finished is on_finished

    asyncio.run(run_case())


def test_since_wait_does_not_drop_signal_between_probe_and_register() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    # Avoid execute-time hello output so since wait starts from empty.
    async def silent_start(request, *, on_output, on_exit):
        return None

    pm.start_command = silent_start  # type: ignore[method-assign]
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo race", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        probe_done = asyncio.Event()
        allow_register = asyncio.Event()

        original_list = service._read_output_repo_list

        async def gated_list(**kwargs):
            result = await original_list(**kwargs)
            probe_done.set()
            await allow_register.wait()
            return result

        service._read_output_repo_list = gated_list  # type: ignore[method-assign]
        waiter = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="since",
                after_chunk_index=-1,
                wait_timeout_ms=2000,
            )
        )
        await asyncio.wait_for(probe_done.wait(), timeout=1)
        # Signal in the window after empty probe returned and before event wait registration.
        await service._handle_output(scope, terminal.terminal_id, command_id, "pty", "late-chunk\n")
        allow_register.set()
        await asyncio.wait_for(waiter, timeout=1)
        chunks = repo.list_output_chunks(terminal.terminal_id, command_id=command_id)
        assert any("late-chunk" in c.chunk for c in chunks)

    asyncio.run(run_case())


def test_multi_waiter_and_cancel_one_waiter() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo multi", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        w1 = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="tail",
                after_chunk_index=None,
                wait_timeout_ms=3000,
            )
        )
        w2 = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="tail",
                after_chunk_index=None,
                wait_timeout_ms=3000,
            )
        )
        await asyncio.sleep(0.05)
        w1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w1
        await pm.finished_cb(command_id, 0, None, "completed")
        await asyncio.wait_for(w2, timeout=1)
        assert repo.get_command(command_id).status == TERMINAL_COMMAND_STATUS_COMPLETED

    asyncio.run(run_case())


def test_no_command_id_wait_returns_immediately() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        started = asyncio.get_running_loop().time()
        result = await service.read_output(
            scope,
            terminal_id=terminal.terminal_id,
            command_id=None,
            mode="tail",
            wait_timeout_ms=2000,
        )
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
        assert elapsed_ms < 500
        assert result.command_finished in (False, None)

    asyncio.run(run_case())


def test_running_command_wait_timeout_stays_running() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="sleep forever", terminal_id=terminal.terminal_id)
        started = asyncio.get_running_loop().time()
        await service._wait_for_read(
            terminal_id=terminal.terminal_id,
            command_id=accepted.command.command_id,
            mode="tail",
            after_chunk_index=None,
            wait_timeout_ms=100,
        )
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
        assert 80 <= elapsed_ms < 800
        command = repo.get_command(accepted.command.command_id)
        assert command.status == TERMINAL_COMMAND_STATUS_RUNNING
        assert repo.get_terminal(terminal.terminal_id).status == "busy"

    asyncio.run(run_case())


def test_timeout_final_status_is_not_completed() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="hang", terminal_id=terminal.terminal_id)
        await pm.finished_cb(accepted.command.command_id, None, "命令执行超时: 10ms", "timeout")
        command = repo.get_command(accepted.command.command_id)
        assert command.status == "timeout"
        assert command.exit_code is None
        assert command.error_message and "超时" in command.error_message

    asyncio.run(run_case())


def test_ready_flushes_prompt_before_registry_cleanup() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=50,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo prompt-bind", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        # Simulate DONE then prompt text still coalescing.
        await pm.finished_cb(command_id, 0, None, "completed")
        await service._handle_output(scope, terminal.terminal_id, command_id, "pty", "PS prompt>")
        assert command_id in service._command_finish_events
        await pm.ready(terminal.terminal_id)
        # READY must flush pending prompt before cleaning registry.
        chunks = repo.list_output_chunks(terminal.terminal_id, command_id=command_id)
        assert any("PS prompt>" in c.chunk for c in chunks)
        assert command_id not in service._command_finish_events
        assert command_id not in service._command_output_versions
        # Late signal must not recreate registry.
        service._signal_command_output(command_id)
        assert command_id not in service._command_output_versions

    asyncio.run(run_case())


def test_eof_wakes_waiters_and_cleans_registry() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo eof", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        waiter = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="tail",
                after_chunk_index=None,
                wait_timeout_ms=3000,
            )
        )
        await asyncio.sleep(0.02)
        # Simulate runtime exit/crash path.
        await service._handle_terminal_exit(scope, terminal.terminal_id, service._runtime_tokens.get(terminal.terminal_id), None, "eof")
        await asyncio.wait_for(waiter, timeout=1)
        assert command_id not in service._command_finish_events or service._command_finish_events[command_id].is_set()

    asyncio.run(run_case())


def test_done_nonzero_preserves_exit_code() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="false", terminal_id=terminal.terminal_id)
        await pm.finished_cb(accepted.command.command_id, 7, None, "failed")
        command = repo.get_command(accepted.command.command_id)
        assert command.status == "failed"
        assert command.exit_code == 7

    asyncio.run(run_case())


def test_flush_barrier_blocks_done_until_inflight_db_persist() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=20,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo barrier", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id

        original_append = service._append_output_chunk

        async def gated_append(**kwargs):
            entered.set()
            await gate.wait()
            return await original_append(**kwargs)

        service._append_output_chunk = gated_append  # type: ignore[method-assign]
        # Coalesce schedules delayed flush; do not await the gated append on this task.
        await service._handle_output(scope, terminal.terminal_id, command_id, "pty", "final-line\n")
        await asyncio.wait_for(entered.wait(), timeout=1)
        # Start DONE while delayed append is gated mid-flight.
        done_task = asyncio.create_task(pm.finished_cb(command_id, 0, None, "completed"))
        await asyncio.sleep(0)
        assert not done_task.done()
        assert repo.get_command(command_id).status == TERMINAL_COMMAND_STATUS_RUNNING
        gate.set()
        await asyncio.wait_for(done_task, timeout=2)
        assert repo.get_command(command_id).status == TERMINAL_COMMAND_STATUS_COMPLETED
        assert any("final-line" in c.chunk for c in repo.chunks)

    asyncio.run(run_case())


def test_since_multi_waiter_cancel_one_still_wakes_on_output() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo multi-since", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        # Baseline after any execute-time output so waiters truly block for a new generation.
        existing = repo.list_output_chunks(terminal.terminal_id, command_id=command_id)
        after = existing[-1].chunk_index if existing else -1
        w1 = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="since",
                after_chunk_index=after,
                wait_timeout_ms=3000,
            )
        )
        w2 = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="since",
                after_chunk_index=after,
                wait_timeout_ms=3000,
            )
        )
        await asyncio.sleep(0.05)
        assert not w1.done() and not w2.done()
        w1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w1
        await service._handle_output(scope, terminal.terminal_id, command_id, "pty", "shared-output\n")
        await asyncio.wait_for(w2, timeout=1)
        assert any("shared-output" in c.chunk for c in repo.chunks)

    asyncio.run(run_case())


def test_execute_failure_cleans_wait_registry() -> None:
    class _BoomPM(_FakeProcessManager):
        async def start_command(self, request, *, on_output, on_exit):
            from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPoolOverloadedError

            raise BlockingPoolOverloadedError(
                pool_name="terminal_control",
                operation="terminal.command.write",
                pending=4,
                max_pending=4,
            )

    repo = _MemoryRepo()
    pm = _BoomPM()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        with pytest.raises(Exception):
            await service.execute_command(scope, command_line="boom", terminal_id=terminal.terminal_id)
        commands = [c for c in repo.commands.values() if c.terminal_id == terminal.terminal_id]
        assert commands and commands[-1].status == "failed"
        command_id = commands[-1].command_id
        assert command_id not in service._command_finish_events
        assert command_id not in service._terminal_wait_commands.get(terminal.terminal_id, set())
        assert repo.get_terminal(terminal.terminal_id).status == "idle"

    asyncio.run(run_case())


def test_ready_watchdog_terminates_without_self_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    manager = pty.PtyTerminalProcessManager()
    closed: list[str] = []
    exits: list[tuple] = []

    async def fake_close(session, *, reason="close"):
        session.closing = True
        closed.append(reason)

    async def on_exit(exit_code, error_message):
        exits.append((exit_code, error_message))

    monkeypatch.setattr(pty, "_SHELL_READY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pty, "_close_terminal_session", fake_close)

    async def run_case() -> None:
        session = pty._TerminalSession(
            terminal_id="term-wd",
            shell="bash",
            on_exit=on_exit,
            runtime_token="rt",
        )
        manager._sessions[session.terminal_id] = session
        session.shell_ready = False
        session.shell_ready_event.clear()
        manager._arm_ready_watchdog(session, "cmd-wd")
        await asyncio.sleep(0.2)
        assert closed
        assert exits
        assert session.ready_watchdog_task is None or session.ready_watchdog_task.done()

    asyncio.run(run_case())


def test_unix_watcher_finally_cleans_paths_even_if_callback_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    script = tmp_path / "managed.bash"
    script.write_text("hi", encoding="utf-8")
    manager = pty.PtyTerminalProcessManager()
    exits: list[int] = []

    async def boom_exit(exit_code, error_message):
        exits.append(1)
        raise RuntimeError("on_exit boom")

    async def run_case() -> None:
        session = pty._TerminalSession(
            terminal_id="term-clean",
            shell="bash",
            on_exit=boom_exit,
            integration_paths=[str(script)],
            master_fd=None,
            process=None,
        )
        manager._sessions[session.terminal_id] = session
        # Drive watcher finally path with missing process/fd.
        await manager._watch_unix_terminal(session)
        assert exits == [1]
        assert session.integration_paths == []
        assert not script.exists()
        assert session.exit_notified is True

    asyncio.run(run_case())


def test_fail_active_command_through_service_marks_failed_and_wakes() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="crash", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        waiter = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="tail",
                after_chunk_index=None,
                wait_timeout_ms=2000,
            )
        )
        await asyncio.sleep(0.02)
        await pm.finished_cb(command_id, None, "shell crashed", "failed")
        await asyncio.wait_for(waiter, timeout=1)
        command = repo.get_command(command_id)
        assert command.status == "failed"
        assert command.error_message and "crashed" in command.error_message

    asyncio.run(run_case())


def test_cancel_after_db_commit_does_not_duplicate_chunk() -> None:
    from agent_runtime.terminal.terminal_service import _PendingTerminalOutput

    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="echo once", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        committed = asyncio.Event()
        release_persist_result = asyncio.Event()
        pending_key = (terminal.terminal_id, command_id, "pty")
        service._pending_outputs[pending_key] = _PendingTerminalOutput(
            scope=scope,
            terminal_id=terminal.terminal_id,
            command_id=command_id,
            stream="pty",
            parts=["only-once\n"],
            byte_offset=0,
            chunk_count=1,
            pending_bytes=len("only-once\n".encode("utf-8")),
        )
        original_append = service._append_output_chunk

        async def committed_but_not_returned(**kwargs):
            result = await original_append(**kwargs)
            committed.set()
            await release_persist_result.wait()
            return result

        service._append_output_chunk = committed_but_not_returned  # type: ignore[method-assign]
        flush_task = asyncio.create_task(service._flush_pending_output(pending_key))
        await asyncio.wait_for(committed.wait(), timeout=1)
        flush_task.cancel()
        await asyncio.sleep(0)
        assert not flush_task.done()
        release_persist_result.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(flush_task, timeout=1)
        await service._flush_command_outputs(terminal.terminal_id, command_id)
        matches = [c for c in repo.chunks if "only-once" in c.chunk]
        assert len(matches) == 1
        assert pending_key not in service._pending_outputs

    asyncio.run(run_case())


def test_execute_cancelled_error_cleans_state() -> None:
    class _CancelPM(_FakeProcessManager):
        def __init__(self) -> None:
            super().__init__()
            self.write_entered = asyncio.Event()
            self.never_finishes = asyncio.Event()
            self.terminated: list[str] = []

        async def start_command(self, request, *, on_output, on_exit):
            self.write_entered.set()
            await self.never_finishes.wait()

        async def terminate_terminal(self, terminal_id: str) -> None:
            self.terminated.append(terminal_id)

    repo = _MemoryRepo()
    pm = _CancelPM()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        execute_task = asyncio.create_task(
            service.execute_command(scope, command_line="cancel-me", terminal_id=terminal.terminal_id)
        )
        await asyncio.wait_for(pm.write_entered.wait(), timeout=1)
        execute_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execute_task, timeout=1)
        commands = [c for c in repo.commands.values() if c.terminal_id == terminal.terminal_id]
        assert commands and commands[-1].status == "cancelled"
        assert commands[-1].command_id not in service._command_finish_events
        assert terminal.terminal_id in pm.terminated
        assert repo.get_terminal(terminal.terminal_id).status in {"error", "idle"}

    asyncio.run(run_case())


def test_execute_indeterminate_error_marks_error_and_terminates() -> None:
    class _IndeterminatePM(_FakeProcessManager):
        def __init__(self):
            super().__init__()
            self.terminated: list[str] = []

        async def start_command(self, request, *, on_output, on_exit):
            raise RuntimeError("windows pty control timed out")

        async def terminate_terminal(self, terminal_id: str) -> None:
            self.terminated.append(terminal_id)

    repo = _MemoryRepo()
    pm = _IndeterminatePM()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        with pytest.raises(RuntimeError):
            await service.execute_command(scope, command_line="maybe-written", terminal_id=terminal.terminal_id)
        assert terminal.terminal_id in pm.terminated
        assert repo.get_terminal(terminal.terminal_id).status == "error"
        commands = [c for c in repo.commands.values() if c.terminal_id == terminal.terminal_id]
        assert commands[-1].status == "failed"
        assert commands[-1].command_id not in service._command_finish_events

    asyncio.run(run_case())


def test_close_busy_terminal_wakes_and_clears_registry() -> None:
    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        max_output_chars_per_read=50000,
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="sleep", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        waiter = asyncio.create_task(
            service._wait_for_read(
                terminal_id=terminal.terminal_id,
                command_id=command_id,
                mode="tail",
                after_chunk_index=None,
                wait_timeout_ms=3000,
            )
        )
        await asyncio.sleep(0.02)
        # Simulate command finished but still awaiting READY, then close.
        await pm.finished_cb(command_id, 0, None, "completed")
        await service.close_terminal(scope, terminal.terminal_id)
        await asyncio.wait_for(waiter, timeout=1)
        assert command_id not in service._command_finish_events
        assert command_id not in service._command_output_events
        assert command_id not in service._command_output_versions
        assert terminal.terminal_id not in service._terminal_wait_commands
        assert not any(k[0] == terminal.terminal_id for k in service._inflight_output_flushes)

    asyncio.run(run_case())


def test_watchdog_exit_notify_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_runtime.terminal.infrastructure import pty_terminal_process as pty

    manager = pty.PtyTerminalProcessManager()
    exits: list[tuple] = []

    async def on_exit(exit_code, error_message):
        exits.append((exit_code, error_message))

    async def fake_close(session, *, reason="close"):
        session.closing = True
        # Simulate watcher already notifying during terminate.
        await manager._notify_terminal_exit(session, None, "watcher")

    monkeypatch.setattr(pty, "_SHELL_READY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pty, "_close_terminal_session", fake_close)

    async def run_case() -> None:
        session = pty._TerminalSession(
            terminal_id="term-once",
            shell="bash",
            on_exit=on_exit,
            runtime_token="rt",
        )
        manager._sessions[session.terminal_id] = session
        manager._arm_ready_watchdog(session, "cmd-once")
        await asyncio.sleep(0.2)
        assert len(exits) == 1

    asyncio.run(run_case())


def test_close_terminal_does_not_publish_detached_twice_when_watcher_reports_exit() -> None:
    class _ExitOnTerminatePM(_FakeProcessManager):
        def __init__(self) -> None:
            super().__init__()
            self.exit_callback = None

        async def start_terminal(
            self, request, *, on_exit, on_shell_ready=None, on_command_finished=None
        ):
            self.exit_callback = on_exit
            if on_shell_ready is not None:
                await on_shell_ready(request.terminal_id)
            return True

        async def terminate_terminal(self, terminal_id: str) -> None:
            assert self.exit_callback is not None
            await self.exit_callback(0, None)

    class _RecordingCommunicationGateway:
        def __init__(self) -> None:
            self.message_types: list[str] = []

        async def publish_message(self, request) -> None:
            self.message_types.append(request.message_type)

    repo = _MemoryRepo()
    pm = _ExitOnTerminatePM()
    communication = _RecordingCommunicationGateway()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        communication_gateway=communication,
        default_shell=platform_supported_shell(),
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        communication_stream_id="stream-1",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        await service.close_terminal(scope, terminal.terminal_id)
        assert communication.message_types.count("terminal.runtime.detached") == 1

    asyncio.run(run_case())


def test_persisted_output_wakes_since_before_slow_event_publish_finishes() -> None:
    class _BlockingOutputGateway:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def publish_message(self, request) -> None:
            if request.message_type == "terminal.output.delta":
                self.entered.set()
                await self.release.wait()

    repo = _MemoryRepo()
    pm = _FakeProcessManager()
    service = TerminalService(
        repository=repo,
        process_manager=pm,
        scope_resolver=TerminalRuntimeScopeResolver(),
        default_shell=platform_supported_shell(),
        output_coalesce_interval_ms=0,
    )
    scope = TerminalRuntimeScope(
        owner_type="task",
        owner_id="task-1",
        default_cwd="D:/workspace",
        communication_stream_id="stream-1",
        agent_instance_id="agent-1",
        task_id="task-1",
    )

    async def run_case() -> None:
        terminal = await service.create_terminal(scope, cwd="D:/workspace", shell=platform_supported_shell())
        accepted = await service.execute_command(scope, command_line="interactive", terminal_id=terminal.terminal_id)
        command_id = accepted.command.command_id
        baseline_version = service._command_output_versions[command_id]
        baseline_event = service._command_output_events[command_id]
        gateway = _BlockingOutputGateway()
        service._communication_gateway = gateway
        output_task = asyncio.create_task(
            service._handle_output(scope, terminal.terminal_id, command_id, "pty", "new-output\n")
        )
        await asyncio.wait_for(gateway.entered.wait(), timeout=1)
        assert baseline_event.is_set()
        assert service._command_output_versions[command_id] == baseline_version + 1
        assert not output_task.done()
        gateway.release.set()
        await asyncio.wait_for(output_task, timeout=1)

    asyncio.run(run_case())
