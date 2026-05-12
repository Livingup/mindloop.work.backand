from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

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
            process = await asyncio.create_subprocess_exec(
                *_shell_argv(request.shell, request.command_line),
                cwd=request.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
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

    async def terminate_terminal(self, terminal_id: str) -> None:
        running = self._processes.get(terminal_id)
        if running is None:
            return
        process = running.process
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
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
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
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
    while True:
        data = await reader.read(4096)
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        if text:
            await on_output(stream_name, text)


def _shell_argv(shell: str, command_line: str) -> list[str]:
    normalized = shell.strip().lower()
    if normalized in {"powershell", "powershell.exe"}:
        return ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command_line]
    if normalized in {"pwsh", "pwsh.exe"}:
        return ["pwsh", "-NoLogo", "-NoProfile", "-Command", command_line]
    if normalized in {"cmd", "cmd.exe"}:
        return ["cmd", "/d", "/s", "/c", command_line]
    if normalized in {"bash", "bash.exe"}:
        return ["bash", "-lc", command_line]
    if normalized in {"sh", "sh.exe"}:
        return ["sh", "-lc", command_line]
    if normalized == "default":
        return _default_shell_argv(command_line)
    return [shell, command_line]


def _default_shell_argv(command_line: str) -> list[str]:
    if sys.platform.startswith("win"):
        comspec = os.environ.get("COMSPEC") or "cmd"
        if "powershell" in comspec.lower() or "pwsh" in comspec.lower():
            return [comspec, "-NoLogo", "-NoProfile", "-Command", command_line]
        return [comspec, "/d", "/s", "/c", command_line]
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-lc", command_line]
