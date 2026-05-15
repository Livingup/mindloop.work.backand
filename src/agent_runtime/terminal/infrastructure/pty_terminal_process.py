from __future__ import annotations

import asyncio
import codecs
import locale
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Pattern

from agent_runtime.terminal.terminal_process import (
    TerminalExitHandler,
    TerminalOutputHandler,
    TerminalProcessManager,
    TerminalProcessStartRequest,
    TerminalSessionStartRequest,
)

_SENTINEL_PREFIX = "__AGENT_RUNTIME_COMMAND_DONE__"


@dataclass(slots=True)
class _ActiveCommand:
    command_id: str
    on_output: TerminalOutputHandler
    on_exit: TerminalExitHandler
    sentinel_pattern: Pattern[str]
    command_echo: str
    timeout_task: asyncio.Task[None] | None = None
    pending_text: str = ""
    echo_stripped: bool = False


@dataclass(slots=True)
class _TerminalSession:
    terminal_id: str
    shell: str
    on_exit: TerminalExitHandler
    runtime_token: str | None = None
    process: asyncio.subprocess.Process | None = None
    master_fd: int | None = None
    winpty: Any | None = None
    task: asyncio.Task[None] | None = None
    active_command: _ActiveCommand | None = None
    closing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PtyTerminalProcessManager(TerminalProcessManager):
    def __init__(self) -> None:
        self._sessions: dict[str, _TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def start_terminal(
        self,
        request: TerminalSessionStartRequest,
        *,
        on_exit: TerminalExitHandler,
    ) -> bool:
        async with self._lock:
            existing = self._sessions.get(request.terminal_id)
            if existing is not None and _session_alive(existing):
                return False
            if existing is not None:
                self._sessions.pop(request.terminal_id, None)
            if sys.platform.startswith("win"):
                session = await _start_windows_terminal(request, on_exit=on_exit)
                session.task = asyncio.create_task(self._watch_windows_terminal(session))
            else:
                session = await _start_unix_terminal(request, on_exit=on_exit)
                session.task = asyncio.create_task(self._watch_unix_terminal(session))
            self._sessions[request.terminal_id] = session
            return True

    async def start_command(
        self,
        request: TerminalProcessStartRequest,
        *,
        on_output: TerminalOutputHandler,
        on_exit: TerminalExitHandler,
    ) -> None:
        session = self._require_session(request.terminal_id)
        async with session.lock:
            if session.active_command is not None:
                raise ValueError(f"终端已有命令在执行: {request.terminal_id}")
            command_text = _wrap_command(session.shell, request.command_id, request.command_line)
            active = _ActiveCommand(
                command_id=request.command_id,
                on_output=on_output,
                on_exit=on_exit,
                sentinel_pattern=_sentinel_pattern(request.command_id),
                command_echo=command_text,
            )
            session.active_command = active
            if request.timeout_ms is not None and request.timeout_ms > 0:
                active.timeout_task = asyncio.create_task(self._enforce_timeout(session, request.command_id, request.timeout_ms))
            try:
                await _write_terminal(session, command_text + "\r")
            except Exception:
                session.active_command = None
                if active.timeout_task is not None:
                    active.timeout_task.cancel()
                raise

    async def write_input(self, terminal_id: str, text: str) -> int:
        session = self._require_session(terminal_id)
        payload = _normalize_terminal_input(text)
        await _write_terminal(session, payload)
        return len(payload.encode("utf-8", errors="replace"))

    async def interrupt_terminal(self, terminal_id: str) -> None:
        session = self._require_session(terminal_id)
        await _write_terminal(session, "\x03")
        active = session.active_command
        if active is not None:
            asyncio.create_task(self._finalize_interrupted_command(session, active.command_id))

    async def terminate_terminal(self, terminal_id: str) -> None:
        session = self._sessions.get(terminal_id)
        if session is None:
            return
        await _close_terminal_session(session)
        if session.task is not None:
            try:
                await asyncio.wait_for(session.task, timeout=3)
            except asyncio.TimeoutError:
                session.task.cancel()
                try:
                    await session.task
                except asyncio.CancelledError:
                    pass
        self._discard_session_if_current(session)

    async def shutdown(self) -> None:
        for terminal_id in list(self._sessions.keys()):
            await self.terminate_terminal(terminal_id)

    def _require_session(self, terminal_id: str) -> _TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None or not _session_alive(session):
            raise KeyError(f"终端运行时未挂载或已退出: {terminal_id}")
        return session

    def _discard_session_if_current(self, session: _TerminalSession) -> None:
        if self._sessions.get(session.terminal_id) is session:
            self._sessions.pop(session.terminal_id, None)

    async def _watch_unix_terminal(self, session: _TerminalSession) -> None:
        decoder = _StreamTextDecoder()
        exit_code: int | None = None
        error_message: str | None = None
        try:
            if session.master_fd is None or session.process is None:
                raise RuntimeError("Unix PTY 终端未初始化。")
            while session.process.returncode is None:
                data = await _read_unix_pty(session.master_fd)
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    await self._handle_terminal_output(session, text)
            text = decoder.flush()
            if text:
                await self._handle_terminal_output(session, text)
            exit_code = await session.process.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_message = None if session.closing else str(exc)
        finally:
            self._discard_session_if_current(session)
            await self._fail_active_command(session, error_message or "终端运行时已退出。")
            await session.on_exit(exit_code, error_message)
            if session.master_fd is not None:
                try:
                    os.close(session.master_fd)
                except OSError:
                    pass

    async def _watch_windows_terminal(self, session: _TerminalSession) -> None:
        exit_code: int | None = None
        error_message: str | None = None
        try:
            if session.winpty is None:
                raise RuntimeError("Windows ConPTY 终端未初始化。")
            while _session_alive(session):
                text = await asyncio.to_thread(_read_windows_pty, session.winpty)
                if text:
                    await self._handle_terminal_output(session, text)
                else:
                    await asyncio.sleep(0.05)
            exit_code = _windows_exit_status(session.winpty)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_message = None if session.closing else str(exc)
        finally:
            self._discard_session_if_current(session)
            await self._fail_active_command(session, error_message or "终端运行时已退出。")
            await session.on_exit(exit_code, error_message)

    async def _handle_terminal_output(self, session: _TerminalSession, text: str) -> None:
        active = session.active_command
        if active is None:
            return
        active.pending_text += text
        _strip_command_echo(active)
        match = active.sentinel_pattern.search(active.pending_text)
        if match is None:
            keep_tail = len(_SENTINEL_PREFIX) + len(active.command_id) + 32
            if len(active.pending_text) <= keep_tail:
                return
            emit_text = active.pending_text[:-keep_tail]
            emit_text = _remove_internal_command_echo(emit_text)
            active.pending_text = active.pending_text[-keep_tail:]
            if emit_text:
                await active.on_output("pty", emit_text)
            return
        before = active.pending_text[: match.start()]
        before = _remove_internal_command_echo(before)
        if before:
            await active.on_output("pty", before)
        exit_code = int(match.group(1))
        await self._complete_active_command(session, active.command_id, exit_code=exit_code, error_message=None)

    async def _complete_active_command(
        self,
        session: _TerminalSession,
        command_id: str,
        *,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        active = session.active_command
        if active is None or active.command_id != command_id:
            return
        session.active_command = None
        if active.timeout_task is not None:
            active.timeout_task.cancel()
        await active.on_exit(exit_code, error_message)

    async def _fail_active_command(self, session: _TerminalSession, error_message: str) -> None:
        active = session.active_command
        if active is None:
            return
        session.active_command = None
        if active.timeout_task is not None:
            active.timeout_task.cancel()
        if active.pending_text:
            await active.on_output("pty", active.pending_text)
        await active.on_exit(None, error_message)

    async def _enforce_timeout(self, session: _TerminalSession, command_id: str, timeout_ms: int) -> None:
        try:
            await asyncio.sleep(timeout_ms / 1000)
            active = session.active_command
            if active is None or active.command_id != command_id:
                return
            await _write_terminal(session, "\x03")
            await asyncio.sleep(3)
            active = session.active_command
            if active is not None and active.command_id == command_id:
                await self._complete_active_command(
                    session,
                    command_id,
                    exit_code=None,
                    error_message=f"命令执行超时: {timeout_ms}ms",
                )
                await _close_terminal_session(session)
        except asyncio.CancelledError:
            raise

    async def _finalize_interrupted_command(self, session: _TerminalSession, command_id: str) -> None:
        await asyncio.sleep(0.5)
        active = session.active_command
        if active is None or active.command_id != command_id:
            return
        try:
            await _write_terminal(session, _interrupt_completion_command(session.shell, command_id) + "\r")
        except Exception:
            return
        await asyncio.sleep(5)
        active = session.active_command
        if active is None or active.command_id != command_id:
            return
        await self._complete_active_command(
            session,
            command_id,
            exit_code=None,
            error_message="命令被 Ctrl+C 中断后未返回终端提示，终端运行时已关闭。",
        )
        await _close_terminal_session(session)


async def _start_unix_terminal(request: TerminalSessionStartRequest, *, on_exit: TerminalExitHandler) -> _TerminalSession:
    import fcntl
    import pty
    import struct
    import termios

    master_fd, slave_fd = pty.openpty()
    try:
        rows = max(1, request.rows)
        cols = max(1, request.cols)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        argv = _interactive_shell_argv(request.shell)

        def preexec() -> None:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=request.cwd,
            env=_process_env(),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=preexec,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return _TerminalSession(
        terminal_id=request.terminal_id,
        shell=request.shell,
        on_exit=on_exit,
        runtime_token=request.runtime_token,
        process=process,
        master_fd=master_fd,
    )


async def _start_windows_terminal(request: TerminalSessionStartRequest, *, on_exit: TerminalExitHandler) -> _TerminalSession:
    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise RuntimeError("Windows ConPTY 终端需要安装 pywinpty 依赖。") from exc
    argv = subprocess.list2cmdline(_interactive_shell_argv(request.shell))
    try:
        winpty = PtyProcess.spawn(
            argv,
            cwd=request.cwd,
            env=_process_env(),
            dimensions=(max(1, request.rows), max(1, request.cols)),
        )
    except TypeError:
        winpty = PtyProcess.spawn(argv, cwd=request.cwd, env=_process_env())
    return _TerminalSession(
        terminal_id=request.terminal_id,
        shell=request.shell,
        on_exit=on_exit,
        runtime_token=request.runtime_token,
        winpty=winpty,
    )


async def _read_unix_pty(master_fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def on_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(master_fd, on_ready)
    try:
        await ready
    finally:
        loop.remove_reader(master_fd)
    try:
        return os.read(master_fd, 4096)
    except BlockingIOError:
        return b""
    except OSError:
        return b""


def _read_windows_pty(winpty: Any) -> str:
    try:
        return winpty.read(4096)
    except EOFError:
        return ""


async def _write_terminal(session: _TerminalSession, text: str) -> None:
    if session.master_fd is not None:
        os.write(session.master_fd, text.encode("utf-8", errors="replace"))
        return
    if session.winpty is not None:
        await asyncio.to_thread(session.winpty.write, text)
        return
    raise KeyError(f"终端运行时不可写: {session.terminal_id}")


async def _close_terminal_session(session: _TerminalSession) -> None:
    session.closing = True
    if session.process is not None and session.process.returncode is None:
        try:
            os.killpg(session.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            session.process.terminate()
        try:
            await asyncio.wait_for(session.process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                session.process.kill()
            await session.process.wait()
    if session.winpty is not None:
        try:
            await asyncio.to_thread(session.winpty.close, True)
        except TypeError:
            await asyncio.to_thread(session.winpty.close)
        except Exception:
            try:
                await asyncio.to_thread(session.winpty.kill)
            except Exception:
                pass


def _session_alive(session: _TerminalSession) -> bool:
    if session.process is not None:
        return session.process.returncode is None
    if session.winpty is not None:
        try:
            return bool(session.winpty.isalive())
        except Exception:
            return False
    return False


def _windows_exit_status(winpty: Any) -> int | None:
    try:
        return int(winpty.get_exitstatus())
    except Exception:
        return None


def _normalize_terminal_input(text: str) -> str:
    return text.replace("\r\n", "\r").replace("\n", "\r")


def _sentinel_pattern(command_id: str) -> Pattern[str]:
    return re.compile(re.escape(f"{_SENTINEL_PREFIX}{command_id}__") + r"(-?\d+)")


def _wrap_command(shell: str, command_id: str, command_line: str) -> str:
    sentinel = f"{_SENTINEL_PREFIX}{command_id}__"
    normalized = shell.strip().lower()
    if normalized in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        escaped = sentinel.replace('"', '`"')
        completion = (
            "$agentRuntimeExitCode = if ($null -ne $global:LASTEXITCODE) { $global:LASTEXITCODE } elseif ($?) { 0 } else { 1 }; "
            f"Write-Output \"{escaped}$agentRuntimeExitCode\""
        )
        return f"{command_line}\r{completion}"
    if normalized in {"cmd", "cmd.exe"}:
        return f"{command_line}\recho {sentinel}!ERRORLEVEL!"
    return f"{command_line}\r__agent_runtime_exit_code=$?; printf '\\n{sentinel}%s\\n' \"$__agent_runtime_exit_code\""


def _interrupt_completion_command(shell: str, command_id: str) -> str:
    sentinel = f"{_SENTINEL_PREFIX}{command_id}__130"
    normalized = shell.strip().lower()
    if normalized in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        escaped = sentinel.replace('"', '`"')
        return f"Write-Output \"{escaped}\""
    if normalized in {"cmd", "cmd.exe"}:
        return f"echo {sentinel}"
    return f"printf '\\n{sentinel}\\n'"


def _strip_command_echo(active: _ActiveCommand) -> None:
    if active.echo_stripped:
        return
    index = active.pending_text.find(active.command_echo)
    if index < 0:
        if len(active.pending_text) > len(active.command_echo) + 16:
            active.echo_stripped = True
        return
    end = index + len(active.command_echo)
    while end < len(active.pending_text) and active.pending_text[end] in "\r\n":
        end += 1
    active.pending_text = active.pending_text[:index] + active.pending_text[end:]
    active.echo_stripped = True


def _remove_internal_command_echo(text: str) -> str:
    if not text:
        return text
    cleaned_parts: list[str] = []
    for line in re.split("(\r\n|\r|\n)", text):
        normalized = _strip_ansi(line)
        if (
            "__AGENT_RUNTIME" in normalized
            or "_COMMAND_DONE__" in normalized
            or "agentRuntimeExitCode" in normalized
            or "__agent_runtime_exit_code" in normalized
        ):
            continue
        cleaned_parts.append(line)
    return "".join(cleaned_parts)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


class _StreamTextDecoder:
    def __init__(self) -> None:
        self._encodings = _output_encoding_candidates()
        self._encoding_index = 0
        self._decoder = codecs.getincrementaldecoder(self._encodings[self._encoding_index])(errors="strict")

    def decode(self, data: bytes) -> str:
        while True:
            try:
                return self._decoder.decode(data, final=False)
            except UnicodeDecodeError:
                self._encoding_index += 1
                if self._encoding_index >= len(self._encodings):
                    return data.decode(self._encodings[0], errors="replace")
                self._decoder = codecs.getincrementaldecoder(self._encodings[self._encoding_index])(errors="strict")

    def flush(self) -> str:
        try:
            return self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return b"".decode(self._encodings[0], errors="replace")


def _output_encoding_candidates() -> list[str]:
    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    if sys.platform.startswith("win"):
        oem_encoding = _windows_oem_encoding()
        if oem_encoding:
            encodings.append(oem_encoding)
        encodings.append("mbcs")
    unique: list[str] = []
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized not in {item.lower() for item in unique}:
            unique.append(encoding)
    return unique


def _windows_oem_encoding() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        code_page = ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return None
    if not code_page:
        return None
    return f"cp{code_page}"


def _interactive_shell_argv(shell: str) -> list[str]:
    normalized = shell.strip().lower()
    if normalized in {"powershell", "powershell.exe"}:
        return ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if normalized in {"pwsh", "pwsh.exe"}:
        return ["pwsh", "-NoLogo", "-NoProfile"]
    if normalized in {"cmd", "cmd.exe"}:
        return ["cmd", "/d", "/v:on"]
    if normalized in {"bash", "bash.exe"}:
        return ["bash"]
    if normalized in {"sh", "sh.exe"}:
        return ["sh"]
    if normalized == "default":
        return _default_shell_argv()
    return [shell]


def _default_shell_argv() -> list[str]:
    if sys.platform.startswith("win"):
        return ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    shell = os.environ.get("SHELL") or "bash"
    return [shell]


def _process_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform.startswith("win"):
        env.setdefault("PYTHONUTF8", "1")
    return env
