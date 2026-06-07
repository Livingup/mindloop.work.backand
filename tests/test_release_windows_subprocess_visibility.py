from __future__ import annotations

from types import SimpleNamespace

from agent_runtime.file_tools import workspace_rest_adapter
from agent_runtime.terminal.infrastructure import pty_terminal_process


def test_workspace_git_status_run_git_uses_hidden_subprocess_kwargs(monkeypatch, tmp_path):
    startupinfo = object()
    hidden_kwargs = {"creationflags": 134217728, "startupinfo": startupinfo}
    calls = []

    def fake_hidden_subprocess_kwargs():
        return hidden_kwargs

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="main\n")

    monkeypatch.setattr(workspace_rest_adapter, "hidden_subprocess_kwargs", fake_hidden_subprocess_kwargs)
    monkeypatch.setattr(workspace_rest_adapter.subprocess, "run", fake_run)

    ok, output = workspace_rest_adapter._run_git(tmp_path, "branch", "--show-current")

    assert ok is True
    assert output == "main"
    assert calls
    assert calls[0][0] == ["git", "-C", str(tmp_path), "branch", "--show-current"]
    assert calls[0][1]["creationflags"] == hidden_kwargs["creationflags"]
    assert calls[0][1]["startupinfo"] is startupinfo


def test_terminal_process_env_removes_winpty_show_console_debug_override(monkeypatch):
    monkeypatch.setattr(pty_terminal_process.sys, "platform", "win32")
    monkeypatch.setenv("WINPTY_SHOW_CONSOLE", "1")

    env = pty_terminal_process._process_env()

    assert env["PYTHONUTF8"] == "1"
    assert "WINPTY_SHOW_CONSOLE" not in env
