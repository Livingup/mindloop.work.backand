import asyncio

import pytest

from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, BlockingPoolOverloadedError
from agent_runtime.terminal.infrastructure import pty_terminal_process as pty
from agent_runtime.terminal.terminal_process import TerminalProcessStartRequest



class _FakeWinPty:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.close_calls: list[object] = []
        self.kill_calls = 0

    def isalive(self) -> bool:
        return True

    def write(self, text: str) -> None:
        self.writes.append(text)

    def close(self, *args: object) -> None:
        self.close_calls.append(args)

    def kill(self) -> None:
        self.kill_calls += 1



def _session() -> pty._TerminalSession:
    return pty._TerminalSession(
        terminal_id="term-1",
        shell="powershell",
        on_exit=lambda _exit_code, _error_message: None,
        runtime_token="runtime-1",
        winpty=_FakeWinPty(),
    )



def test_windows_pty_write_uses_bounded_control_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_monitored_to_thread(func, /, *args, **kwargs):
        calls.append({"func": func, "args": args, **kwargs})
        return func(*args)

    monkeypatch.setattr(pty, "monitored_to_thread", fake_monitored_to_thread)
    session = _session()

    asyncio.run(pty._write_terminal(session, "whoami\r", operation="terminal.command.write"))

    assert session.winpty.writes == ["whoami\r"]
    assert len(calls) == 1
    call = calls[0]
    assert call["pool"] == BlockingPool.TERMINAL_CONTROL
    assert call["operation"] == "terminal.command.write"
    assert call["slow_queue_seconds"] == pty._WINDOWS_PTY_CONTROL_SLOW_QUEUE_SECONDS
    assert call["diagnostic_context"]["terminal_id"] == "term-1"
    assert call["diagnostic_context"]["runtime_token"] == "runtime-1"
    assert call["diagnostic_context"]["payload_bytes"] == len("whoami\r".encode("utf-8"))
    assert call["diagnostic_context"]["timeout_seconds"] == pty._WINDOWS_PTY_CONTROL_TIMEOUT_SECONDS



def test_windows_pty_control_timeout_fails_explicitly_and_cancels_await(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def never_finishes(_func, /, *_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(pty, "monitored_to_thread", never_finishes)
    session = _session()

    async def run_case() -> None:
        with pytest.raises(RuntimeError, match="Windows PTY control operation timed out"):
            await pty._run_windows_pty_control(
                session,
                session.winpty.write,
                "blocked\r",
                operation="terminal.command.write",
                caller="unit-test",
                timeout_seconds=0.01,
            )

    asyncio.run(run_case())



def test_start_command_rolls_back_active_command_when_control_pool_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_control_submission(_func, /, *_args, **_kwargs):
        raise BlockingPoolOverloadedError(
            pool_name=BlockingPool.TERMINAL_CONTROL.value,
            operation="terminal.command.write",
            pending=4,
            max_pending=4,
        )

    monkeypatch.setattr(pty, "monitored_to_thread", reject_control_submission)
    manager = pty.PtyTerminalProcessManager()
    session = _session()
    session.shell_ready = True
    session.shell_ready_event.set()
    manager._sessions[session.terminal_id] = session
    exited: list[tuple[int | None, str | None]] = []

    async def run_case() -> None:
        request = TerminalProcessStartRequest(
            terminal_id=session.terminal_id,
            command_id="cmd-1",
            command_line="git status",
            cwd="D:/workspace",
            shell="powershell",
            timeout_ms=None,
        )
        with pytest.raises(RuntimeError, match="Blocking pool overloaded"):
            await manager.start_command(
                request,
                on_output=lambda _stream, _text: None,
                on_exit=lambda exit_code, error_message: exited.append((exit_code, error_message)),
            )

    asyncio.run(run_case())

    assert session.active_command is None
    assert session.shell_ready is True
    assert session.shell_ready_event.is_set()
    assert exited == []



def test_windows_pty_close_falls_back_to_kill_on_bounded_control_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    async def fake_control(func, /, *args, **kwargs):
        calls.append((kwargs["operation"], args))
        if kwargs["operation"] == "terminal.windows_pty.close":
            raise RuntimeError("close failed")
        return func(*args)

    monkeypatch.setattr(pty, "monitored_to_thread", fake_control)
    session = _session()

    asyncio.run(pty._close_terminal_session(session, reason="unit-test"))

    assert session.winpty.close_calls == []
    assert session.winpty.kill_calls == 1
    assert [operation for operation, _args in calls] == [
        "terminal.windows_pty.close",
        "terminal.windows_pty.kill",
    ]



def test_command_input_only_normalizes_newlines_and_appends_cr() -> None:
    assert pty._command_input_text("echo hi") == "echo hi\r"
    assert pty._command_input_text("line1\nline2") == "line1\rline2\r"
    assert pty._command_input_text("already\r") == "already\r"



def test_close_terminal_session_cleans_integration_paths(tmp_path) -> None:
    script = tmp_path / "managed.ps1"
    script.write_text("write-host hi", encoding="utf-8")
    nested = tmp_path / "zdot"
    nested.mkdir()
    (nested / ".zshrc").write_text("PROMPT=x", encoding="utf-8")
    session = _session()
    session.integration_paths = [str(script), str(nested)]
    asyncio.run(pty._close_terminal_session(session, reason="cleanup-test"))
    assert session.integration_paths == []
    assert not script.exists()
    assert not nested.exists()
