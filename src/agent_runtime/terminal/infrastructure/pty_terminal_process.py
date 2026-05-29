from __future__ import annotations

import asyncio
import codecs
import locale
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.terminal.terminal_process import (
    TerminalExitHandler,
    TerminalOutputHandler,
    TerminalProcessManager,
    TerminalProcessStartRequest,
    TerminalSessionStartRequest,
)

_PTY_READ_CHUNK_SIZE = 4096


@dataclass(slots=True)
class _ActiveCommand:
    command_id: str
    on_output: TerminalOutputHandler
    on_exit: TerminalExitHandler
    timeout_task: asyncio.Task[None] | None = None


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
                previous = session.active_command
                if previous.timeout_task is not None:
                    previous.timeout_task.cancel()
            active = _ActiveCommand(
                command_id=request.command_id,
                on_output=on_output,
                on_exit=on_exit,
            )
            session.active_command = active
            if request.timeout_ms is not None and request.timeout_ms > 0:
                active.timeout_task = asyncio.create_task(self._enforce_timeout(session, request.command_id, request.timeout_ms))
            try:
                await _write_terminal(session, _command_input_text(request.command_line))
            except asyncio.CancelledError:
                raise
            except BaseException:
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
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
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
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            error_message = None if session.closing else str(exc)
        finally:
            self._discard_session_if_current(session)
            await self._fail_active_command(session, error_message or "终端运行时已退出。")
            await session.on_exit(exit_code, error_message)

    async def _handle_terminal_output(self, session: _TerminalSession, text: str) -> None:
        active = session.active_command
        if active is None:
            return
        await active.on_output("pty", text)

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
        try:
            winpty = PtyProcess.spawn(argv, cwd=request.cwd, env=_process_env())
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
    except BaseException as exc:
        if _is_process_control_exception(exc):
            raise
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
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
        return os.read(master_fd, _PTY_READ_CHUNK_SIZE)
    except BlockingIOError:
        return b""
    except OSError:
        return b""


def _read_windows_pty(winpty: Any) -> str:
    try:
        return winpty.read(_PTY_READ_CHUNK_SIZE)
    except EOFError:
        return ""
    except BaseException as exc:
        if _is_process_control_exception(exc):
            raise
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc


async def _write_terminal(session: _TerminalSession, text: str) -> None:
    if session.master_fd is not None:
        os.write(session.master_fd, text.encode("utf-8", errors="replace"))
        return
    if session.winpty is not None:
        try:
            await asyncio.to_thread(session.winpty.write, text)
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
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
            try:
                await asyncio.to_thread(session.winpty.close)
            except BaseException as exc:
                if _is_process_control_exception(exc):
                    raise
                try:
                    await asyncio.to_thread(session.winpty.kill)
                except BaseException as kill_exc:
                    if _is_process_control_exception(kill_exc):
                        raise
                    pass
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            try:
                await asyncio.to_thread(session.winpty.kill)
            except BaseException as kill_exc:
                if _is_process_control_exception(kill_exc):
                    raise
                pass


def _session_alive(session: _TerminalSession) -> bool:
    if session.process is not None:
        return session.process.returncode is None
    if session.winpty is not None:
        try:
            return bool(session.winpty.isalive())
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            return False
    return False


def _windows_exit_status(winpty: Any) -> int | None:
    try:
        return int(winpty.get_exitstatus())
    except BaseException as exc:
        if _is_process_control_exception(exc):
            raise
        return None


def _is_process_control_exception(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))


def _normalize_terminal_input(text: str) -> str:
    return text.replace("\r\n", "\r").replace("\n", "\r")


def _command_input_text(command_line: str) -> str:
    command_text = _normalize_terminal_input(command_line)
    return command_text if command_text.endswith("\r") else command_text + "\r"


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
