from __future__ import annotations

import asyncio
import codecs
import locale
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.terminal.terminal_process import (
    TerminalExitHandler,
    TerminalOutputHandler,
    TerminalProcessManager,
    TerminalProcessStartRequest,
)


@dataclass(slots=True)
class _RunningProcess:
    process: asyncio.subprocess.Process
    task: asyncio.Task[None]


class AsyncioSubprocessTerminalProcessManager(TerminalProcessManager):
    def __init__(self) -> None:
        self._processes: dict[str, _RunningProcess] = {}
        self._lock = asyncio.Lock()

    async def start_command(
        self,
        request: TerminalProcessStartRequest,
        *,
        on_output: TerminalOutputHandler,
        on_exit: TerminalExitHandler,
    ) -> None:
        async with self._lock:
            existing = self._processes.get(request.terminal_id)
            if existing is not None and existing.process.returncode is None:
                raise ValueError(f"终端已有命令在执行: {request.terminal_id}")
            process = await _create_subprocess(request)
            task = asyncio.create_task(
                self._watch_process(request, process, on_output=on_output, on_exit=on_exit),
            )
            self._processes[request.terminal_id] = _RunningProcess(process=process, task=task)

    async def write_input(self, terminal_id: str, text: str) -> int:
        running = self._processes.get(terminal_id)
        if running is None or running.process.returncode is not None or running.process.stdin is None:
            raise KeyError(f"终端没有可写入的运行中进程: {terminal_id}")
        data = text.encode("utf-8", errors="replace")
        running.process.stdin.write(data)
        await running.process.stdin.drain()
        return len(data)

    async def interrupt_terminal(self, terminal_id: str) -> None:
        running = self._processes.get(terminal_id)
        if running is None or running.process.returncode is not None:
            raise KeyError(f"终端没有可中断的运行中进程: {terminal_id}")
        try:
            _send_interrupt(running.process)
        except ProcessLookupError:
            return
        except Exception:
            await _terminate_process(running.process, grace_seconds=1)
            return
        try:
            await asyncio.wait_for(running.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            await _force_kill_process(running.process)
            await running.process.wait()

    async def terminate_terminal(self, terminal_id: str) -> None:
        running = self._processes.get(terminal_id)
        if running is None:
            return
        process = running.process
        if process.returncode is None:
            await _terminate_process(process, grace_seconds=3)
        try:
            await asyncio.wait_for(running.task, timeout=3)
        except asyncio.TimeoutError:
            running.task.cancel()
            try:
                await running.task
            except asyncio.CancelledError:
                pass
        self._processes.pop(terminal_id, None)

    async def shutdown(self) -> None:
        for terminal_id in list(self._processes.keys()):
            await self.terminate_terminal(terminal_id)

    async def _watch_process(
        self,
        request: TerminalProcessStartRequest,
        process: asyncio.subprocess.Process,
        *,
        on_output: TerminalOutputHandler,
        on_exit: TerminalExitHandler,
    ) -> None:
        error_message: str | None = None
        try:
            readers = []
            if process.stdout is not None:
                readers.append(asyncio.create_task(_read_stream(process.stdout, "stdout", on_output)))
            if process.stderr is not None:
                readers.append(asyncio.create_task(_read_stream(process.stderr, "stderr", on_output)))
            wait_task = asyncio.create_task(process.wait())
            if request.timeout_ms is not None and request.timeout_ms > 0:
                try:
                    await asyncio.wait_for(wait_task, timeout=request.timeout_ms / 1000)
                except asyncio.TimeoutError:
                    error_message = f"命令执行超时: {request.timeout_ms}ms"
                    await _force_kill_process(process)
                    await process.wait()
            else:
                await wait_task
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            await on_exit(process.returncode, error_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await on_exit(process.returncode, str(exc))
        finally:
            self._processes.pop(request.terminal_id, None)


async def _read_stream(
    reader: asyncio.StreamReader,
    stream_name: str,
    on_output: TerminalOutputHandler,
) -> None:
    decoder = _StreamTextDecoder()
    while True:
        data = await reader.read(4096)
        if not data:
            break
        text = decoder.decode(data)
        if text:
            await on_output(stream_name, text)
    text = decoder.flush()
    if text:
        await on_output(stream_name, text)


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


def _shell_argv(shell: str, command_line: str) -> list[str]:
    normalized = shell.strip().lower()
    if normalized in {"powershell", "powershell.exe"}:
        return [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _powershell_utf8_command(command_line),
        ]
    if normalized in {"pwsh", "pwsh.exe"}:
        return ["pwsh", "-NoLogo", "-NoProfile", "-Command", _powershell_utf8_command(command_line)]
    if normalized in {"cmd", "cmd.exe"}:
        return ["cmd", "/d", "/c", command_line]
    if normalized in {"bash", "bash.exe"}:
        return ["bash", "-lc", command_line]
    if normalized in {"sh", "sh.exe"}:
        return ["sh", "-lc", command_line]
    if normalized == "default":
        return _default_shell_argv(command_line)
    return [shell, command_line]


async def _create_subprocess(request: TerminalProcessStartRequest) -> asyncio.subprocess.Process:
    process_kwargs = _process_kwargs(request)
    if _should_use_windows_cmd_shell(request.shell):
        return await asyncio.create_subprocess_shell(
            request.command_line,
            **process_kwargs,
        )
    return await asyncio.create_subprocess_exec(
        *_shell_argv(request.shell, request.command_line),
        **process_kwargs,
    )


def _process_kwargs(request: TerminalProcessStartRequest) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "cwd": request.cwd,
        "env": _process_env(),
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _send_interrupt(process: asyncio.subprocess.Process) -> None:
    if sys.platform.startswith("win"):
        process.send_signal(signal.CTRL_BREAK_EVENT)
        return
    os.killpg(process.pid, signal.SIGINT)


async def _terminate_process(process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
    if sys.platform.startswith("win"):
        await _force_kill_process(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        await _force_kill_process(process)
        await process.wait()


async def _force_kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform.startswith("win"):
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await killer.wait()
            return
        except Exception:
            pass
    try:
        if sys.platform.startswith("win"):
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _should_use_windows_cmd_shell(shell: str) -> bool:
    if not sys.platform.startswith("win"):
        return False
    normalized = shell.strip().lower()
    if normalized in {"cmd", "cmd.exe"}:
        return True
    if normalized == "default":
        comspec = os.environ.get("COMSPEC") or "cmd"
        return "cmd" in Path(comspec).name.lower()
    return False


def _default_shell_argv(command_line: str) -> list[str]:
    if sys.platform.startswith("win"):
        comspec = os.environ.get("COMSPEC") or "cmd"
        if "powershell" in comspec.lower() or "pwsh" in comspec.lower():
            return [comspec, "-NoLogo", "-NoProfile", "-Command", _powershell_utf8_command(command_line)]
        return [comspec, "/d", "/c", command_line]
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-lc", command_line]


def _powershell_utf8_command(command_line: str) -> str:
    return (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"{command_line}"
    )


def _process_env() -> dict[str, str]:
    env = dict(os.environ)
    if sys.platform.startswith("win"):
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
