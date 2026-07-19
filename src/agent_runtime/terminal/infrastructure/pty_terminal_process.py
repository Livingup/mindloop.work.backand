from __future__ import annotations

import asyncio
import codecs
import locale
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
from agent_runtime.terminal.shell_integration import (
    OrderedSaiStreamParser,
    StreamToken,
    TokenKind,
    integration_script_for_shell,
)
from agent_runtime.terminal.shell_policy import require_shell_executable, resolve_supported_shell
from agent_runtime.terminal.terminal_process import (
    TerminalCommandFinishedHandler,
    TerminalExitHandler,
    TerminalOutputHandler,
    TerminalProcessManager,
    TerminalProcessStartRequest,
    TerminalSessionStartRequest,
    TerminalShellReadyHandler,
)

logger = logging.getLogger(__name__)
_PTY_READ_CHUNK_SIZE = 4096
_WINDOWS_PTY_CONTROL_TIMEOUT_SECONDS = 5.0
_WINDOWS_PTY_CONTROL_SLOW_QUEUE_SECONDS = 0.25
_SHELL_READY_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _ActiveCommand:
    command_id: str
    on_output: TerminalOutputHandler
    on_exit: TerminalExitHandler
    timeout_task: asyncio.Task[None] | None = None
    interrupt_requested: bool = False
    started: bool = False
    completed: bool = False


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
    awaiting_command: _ActiveCommand | None = None
    closing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    osc_parser: OrderedSaiStreamParser = field(default_factory=OrderedSaiStreamParser)
    shell_ready: bool = False
    integration_ready: bool = False
    on_shell_ready: TerminalShellReadyHandler | None = None
    on_command_finished: TerminalCommandFinishedHandler | None = None
    shell_ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_watchdog_task: asyncio.Task[None] | None = None
    exit_notified: bool = False
    integration_paths: list[str] = field(default_factory=list)


class PtyTerminalProcessManager(TerminalProcessManager):
    def __init__(self) -> None:
        self._sessions: dict[str, _TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def start_terminal(
        self,
        request: TerminalSessionStartRequest,
        *,
        on_exit: TerminalExitHandler,
        on_shell_ready: TerminalShellReadyHandler | None = None,
        on_command_finished: TerminalCommandFinishedHandler | None = None,
    ) -> bool:
        started_at = time.perf_counter()
        logger.info(
            "PTY terminal runtime start requested: terminal_id=%s shell=%s cwd=%s runtime_token=%s platform=%s",
            request.terminal_id,
            request.shell,
            request.cwd,
            request.runtime_token,
            sys.platform,
        )
        async with self._lock:
            existing = self._sessions.get(request.terminal_id)
            if existing is not None and _session_alive(existing):
                logger.info(
                    "PTY terminal runtime start skipped because live session already exists: terminal_id=%s runtime_token=%s active_command_id=%s",
                    request.terminal_id,
                    existing.runtime_token,
                    _active_command_id(existing),
                )
                return False
            if existing is not None:
                logger.info(
                    "PTY terminal runtime replacing stale session: terminal_id=%s old_runtime_token=%s active_command_id=%s",
                    request.terminal_id,
                    existing.runtime_token,
                    _active_command_id(existing),
                )
                self._sessions.pop(request.terminal_id, None)
            if sys.platform.startswith("win"):
                session = await _start_windows_terminal(request, on_exit=on_exit)
                watcher = self._watch_windows_terminal
                watcher_type = "winpty"
                task_name = f"terminal-winpty-watch-{request.terminal_id}"
            else:
                session = await _start_unix_terminal(request, on_exit=on_exit)
                watcher = self._watch_unix_terminal
                watcher_type = "pty"
                task_name = f"terminal-pty-watch-{request.terminal_id}"
            # Both platforms must bind command_finished(D) and shell_ready(B) callbacks.
            session.on_shell_ready = on_shell_ready
            session.on_command_finished = on_command_finished
            session.task = get_runtime_task_registry().create_task(
                watcher(session),
                task_name=task_name,
                owner_type="terminal",
                owner_id=request.terminal_id,
                task_class="watcher",
                diagnostic_context={
                    "terminal_id": request.terminal_id,
                    "runtime_token": request.runtime_token,
                    "platform": sys.platform,
                    "watcher_type": watcher_type,
                },
            )
            self._sessions[request.terminal_id] = session
        try:
            await asyncio.wait_for(session.shell_ready_event.wait(), timeout=_SHELL_READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            await self.terminate_terminal(request.terminal_id)
            raise RuntimeError(
                f"Shell Integration 在 {_SHELL_READY_TIMEOUT_SECONDS:.0f}s 内未收到 READY(B): "
                f"terminal_id={request.terminal_id} shell={request.shell!r}"
            ) from exc
        logger.info(
            "PTY terminal runtime started: terminal_id=%s shell=%s cwd=%s runtime_token=%s watcher_task=%s duration_ms=%s",
            request.terminal_id,
            request.shell,
            request.cwd,
            request.runtime_token,
            session.task.get_name() if session.task is not None else None,
            int((time.perf_counter() - started_at) * 1000),
        )
        return True

    async def start_command(
        self,
        request: TerminalProcessStartRequest,
        *,
        on_output: TerminalOutputHandler,
        on_exit: TerminalExitHandler,
    ) -> None:
        started_at = time.perf_counter()
        logger.info(
            "PTY command start requested: terminal_id=%s command_id=%s cwd=%s shell=%s timeout_ms=%s command_chars=%s",
            request.terminal_id,
            request.command_id,
            request.cwd,
            request.shell,
            request.timeout_ms,
            len(request.command_line),
        )
        session = self._require_session(request.terminal_id)
        if not session.shell_ready:
            try:
                await asyncio.wait_for(session.shell_ready_event.wait(), timeout=_SHELL_READY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"终端尚未 shell-ready，不能执行命令: terminal_id={request.terminal_id}"
                ) from exc
        async with session.lock:
            if session.active_command is not None and not session.active_command.completed:
                raise RuntimeError(
                    f"终端仍有未完成的前台命令，禁止并发写入: terminal_id={request.terminal_id} "
                    f"active_command_id={session.active_command.command_id} new_command_id={request.command_id}"
                )
            # Outstanding command occupies the shell until finished event clears shell_ready.
            session.shell_ready = False
            session.shell_ready_event.clear()
            active = _ActiveCommand(
                command_id=request.command_id,
                on_output=on_output,
                on_exit=on_exit,
            )
            session.active_command = active
            if request.timeout_ms is not None and request.timeout_ms > 0:
                active.timeout_task = get_runtime_task_registry().create_task(
                    self._enforce_timeout(session, request.command_id, request.timeout_ms),
                    task_name=f"terminal-timeout-{request.terminal_id}-{request.command_id}",
                    owner_type="terminal",
                    owner_id=request.terminal_id,
                    task_class="command_timeout",
                    diagnostic_context={
                        "terminal_id": request.terminal_id,
                        "command_id": request.command_id,
                        "timeout_ms": request.timeout_ms,
                        "command_chars": len(request.command_line),
                    },
                )
            try:
                # Do not rewrite LLM command_line; only normalize newlines for PTY.
                await _write_terminal(session, _command_input_text(request.command_line), operation="terminal.command.write")
            except asyncio.CancelledError:
                session.active_command = None
                if active.timeout_task is not None:
                    get_runtime_task_registry().cancel(
                        active.timeout_task,
                        message="terminal command start cancelled",
                        diagnostic_context={"terminal_id": request.terminal_id, "command_id": request.command_id},
                    )
                # Cancel during write is indeterminate on Windows control pool; do not restore ready.
                logger.warning(
                    "PTY command start cancelled while writing command input: terminal_id=%s command_id=%s duration_ms=%s",
                    request.terminal_id,
                    request.command_id,
                    int((time.perf_counter() - started_at) * 1000),
                )
                raise
            except BaseException as exc:
                session.active_command = None
                if active.timeout_task is not None:
                    get_runtime_task_registry().cancel(
                        active.timeout_task,
                        message="terminal command start failed",
                        diagnostic_context={"terminal_id": request.terminal_id, "command_id": request.command_id},
                    )
                if _is_definite_unwritten_error(exc):
                    # Known not submitted to PTY (e.g. control pool overload).
                    session.shell_ready = True
                    session.shell_ready_event.set()
                # Otherwise leave not-ready; service must terminate runtime to avoid split-brain.
                logger.exception(
                    "PTY command start failed while writing command input: terminal_id=%s command_id=%s duration_ms=%s",
                    request.terminal_id,
                    request.command_id,
                    int((time.perf_counter() - started_at) * 1000),
                )
                raise
            logger.info(
                "PTY command start accepted: terminal_id=%s command_id=%s duration_ms=%s",
                request.terminal_id,
                request.command_id,
                int((time.perf_counter() - started_at) * 1000),
            )

    async def write_input(self, terminal_id: str, text: str) -> int:
        started_at = time.perf_counter()
        session = self._require_session(terminal_id)
        payload = _normalize_terminal_input(text)
        logger.info(
            "PTY terminal input write requested: terminal_id=%s active_command_id=%s input_chars=%s payload_bytes=%s",
            terminal_id,
            _active_command_id(session),
            len(text),
            len(payload.encode("utf-8", errors="replace")),
        )
        await _write_terminal(session, payload, operation="terminal.input.write")
        bytes_written = len(payload.encode("utf-8", errors="replace"))
        logger.info(
            "PTY terminal input write completed: terminal_id=%s active_command_id=%s bytes_written=%s duration_ms=%s",
            terminal_id,
            _active_command_id(session),
            bytes_written,
            int((time.perf_counter() - started_at) * 1000),
        )
        return bytes_written

    async def interrupt_terminal(self, terminal_id: str) -> None:
        started_at = time.perf_counter()
        session = self._require_session(terminal_id)
        active = session.active_command
        if active is not None and not active.completed:
            active.interrupt_requested = True
        logger.info(
            "PTY terminal interrupt requested: terminal_id=%s active_command_id=%s",
            terminal_id,
            _active_command_id(session),
        )
        await _write_terminal(session, "\x03", operation="terminal.interrupt.write")
        logger.info(
            "PTY terminal interrupt completed: terminal_id=%s active_command_id=%s duration_ms=%s",
            terminal_id,
            _active_command_id(session),
            int((time.perf_counter() - started_at) * 1000),
        )

    async def terminate_terminal(self, terminal_id: str) -> None:
        started_at = time.perf_counter()
        session = self._sessions.get(terminal_id)
        if session is None:
            logger.info("PTY terminal terminate skipped because session is missing: terminal_id=%s", terminal_id)
            return
        logger.info(
            "PTY terminal terminate requested: terminal_id=%s runtime_token=%s active_command_id=%s closing=%s watcher_task=%s",
            terminal_id,
            session.runtime_token,
            _active_command_id(session),
            session.closing,
            session.task.get_name() if session.task is not None else None,
        )
        self._cancel_ready_watchdog(session, message="terminal terminate")
        await _close_terminal_session(session)
        if session.task is not None:
            try:
                await asyncio.wait_for(session.task, timeout=3)
            except asyncio.TimeoutError:
                logger.warning(
                    "PTY terminal watcher did not stop after close; cancelling: terminal_id=%s watcher_task=%s",
                    terminal_id,
                    session.task.get_name(),
                )
                get_runtime_task_registry().cancel(
                    session.task,
                    message="terminal watcher terminate timeout",
                    diagnostic_context={"terminal_id": terminal_id, "runtime_token": session.runtime_token},
                )
                try:
                    await session.task
                except asyncio.CancelledError:
                    pass
        self._discard_session_if_current(session)
        logger.info(
            "PTY terminal terminate completed: terminal_id=%s runtime_token=%s duration_ms=%s",
            terminal_id,
            session.runtime_token,
            int((time.perf_counter() - started_at) * 1000),
        )

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
                    get_runtime_task_registry().mark_progress(
                        asyncio.current_task() or f"terminal-pty-watch-{session.terminal_id}",
                        diagnostic_context={
                            "terminal_id": session.terminal_id,
                            "active_command_id": _active_command_id(session),
                            "text_chars": len(text),
                            "watcher_type": "pty",
                        },
                    )
                    await self._handle_terminal_output(session, text)
            text = decoder.flush()
            if text:
                get_runtime_task_registry().mark_progress(
                    asyncio.current_task() or f"terminal-pty-watch-{session.terminal_id}",
                    diagnostic_context={
                        "terminal_id": session.terminal_id,
                        "active_command_id": _active_command_id(session),
                        "text_chars": len(text),
                        "watcher_type": "pty_flush",
                    },
                )
                await self._handle_terminal_output(session, text)
            exit_code = await session.process.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            error_message = None if session.closing else str(exc)
        finally:
            self._cancel_ready_watchdog(session, message="unix watcher exit")
            self._discard_session_if_current(session)
            try:
                try:
                    await self._fail_active_command(session, error_message or "终端运行时已退出。")
                except Exception:
                    logger.exception(
                        "Unix watcher failed while failing active command: terminal_id=%s",
                        session.terminal_id,
                    )
                try:
                    await self._notify_terminal_exit(session, exit_code, error_message)
                except Exception:
                    logger.exception(
                        "Unix watcher failed while notifying terminal exit: terminal_id=%s",
                        session.terminal_id,
                    )
            finally:
                if session.master_fd is not None:
                    try:
                        os.close(session.master_fd)
                    except OSError:
                        pass
                    session.master_fd = None
                _cleanup_integration_paths(session)

    async def _watch_windows_terminal(self, session: _TerminalSession) -> None:
        exit_code: int | None = None
        error_message: str | None = None
        try:
            if session.winpty is None:
                raise RuntimeError("Windows ConPTY 终端未初始化。")
            logger.info(
                "Windows PTY watcher started: terminal_id=%s runtime_token=%s active_command_id=%s",
                session.terminal_id,
                session.runtime_token,
                _active_command_id(session),
            )
            while _session_alive(session):
                text = await monitored_to_thread(
                    _read_windows_pty,
                    session.winpty,
                    operation="terminal.windows_pty.read",
                    caller="PtyTerminalProcessManager._watch_windows_terminal",
                    pool=BlockingPool.TERMINAL_READ,
                    log=logger,
                    diagnostic_context={
                        "terminal_id": session.terminal_id,
                        "runtime_token": session.runtime_token,
                        "active_command_id": _active_command_id(session),
                    },
                )
                if text:
                    logger.debug(
                        "Windows PTY watcher received output: terminal_id=%s active_command_id=%s text_chars=%s",
                        session.terminal_id,
                        _active_command_id(session),
                        len(text),
                    )
                    get_runtime_task_registry().mark_progress(
                        asyncio.current_task() or f"terminal-winpty-watch-{session.terminal_id}",
                        diagnostic_context={
                            "terminal_id": session.terminal_id,
                            "active_command_id": _active_command_id(session),
                            "text_chars": len(text),
                            "watcher_type": "winpty",
                        },
                    )
                    await self._handle_terminal_output(session, text)
                else:
                    await asyncio.sleep(0.05)
            exit_code = _windows_exit_status(session.winpty)
        except asyncio.CancelledError:
            logger.warning(
                "Windows PTY watcher cancelled: terminal_id=%s runtime_token=%s active_command_id=%s",
                session.terminal_id,
                session.runtime_token,
                _active_command_id(session),
            )
            raise
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            error_message = None if session.closing else str(exc)
            logger.warning(
                "Windows PTY watcher failed: terminal_id=%s runtime_token=%s active_command_id=%s closing=%s error_type=%s error=%s",
                session.terminal_id,
                session.runtime_token,
                _active_command_id(session),
                session.closing,
                exc.__class__.__name__,
                str(exc),
            )
        finally:
            logger.info(
                "Windows PTY watcher finalizing: terminal_id=%s runtime_token=%s active_command_id=%s exit_code=%s error_message=%s closing=%s",
                session.terminal_id,
                session.runtime_token,
                _active_command_id(session),
                exit_code,
                error_message,
                session.closing,
            )
            self._cancel_ready_watchdog(session, message="windows watcher exit")
            self._discard_session_if_current(session)
            try:
                try:
                    await self._fail_active_command(session, error_message or "终端运行时已退出。")
                except Exception:
                    logger.exception(
                        "Windows watcher failed while failing active command: terminal_id=%s",
                        session.terminal_id,
                    )
                try:
                    await self._notify_terminal_exit(session, exit_code, error_message)
                except Exception:
                    logger.exception(
                        "Windows watcher failed while notifying terminal exit: terminal_id=%s",
                        session.terminal_id,
                    )
            finally:
                _cleanup_integration_paths(session)

    async def _handle_terminal_output(self, session: _TerminalSession, text: str) -> None:
        tokens = session.osc_parser.feed(text)
        for token in tokens:
            await self._handle_stream_token(session, token)

    async def _handle_stream_token(self, session: _TerminalSession, token: StreamToken) -> None:
        if token.kind == TokenKind.VISIBLE:
            target = session.active_command or session.awaiting_command
            # awaiting_command is already marked completed but still owns prompt text until READY.
            if target is not None and token.text:
                await target.on_output("pty", token.text)
            return
        if token.kind == TokenKind.DONE:
            active = session.active_command
            if active is None or active.completed:
                return
            final_status = "completed"
            error_message = None
            exit_code = token.exit_code if token.exit_code_present else None
            if active.interrupt_requested:
                final_status = "cancelled"
                error_message = "command interrupted by ctrl_c"
            elif not token.exit_code_present:
                final_status = "failed"
                error_message = "DONE frame missing status"
            elif exit_code != 0:
                final_status = "failed"
            # Move to awaiting-ready so prompt text after D still binds to this command.
            active.completed = True
            session.awaiting_command = active
            session.active_command = None
            if active.timeout_task is not None:
                get_runtime_task_registry().cancel(
                    active.timeout_task,
                    message="terminal command completed",
                    diagnostic_context={"terminal_id": session.terminal_id, "command_id": active.command_id},
                )
                active.timeout_task = None
            if session.on_command_finished is not None:
                await session.on_command_finished(active.command_id, exit_code, error_message, final_status)
            else:
                await active.on_exit(exit_code, error_message)
            self._arm_ready_watchdog(session, active.command_id)
            return
        if token.kind == TokenKind.READY:
            self._cancel_ready_watchdog(session, message="shell ready received")
            session.integration_ready = True
            session.shell_ready = True
            session.shell_ready_event.set()
            # Clear awaiting binding only after READY so D..B prompt text is kept.
            session.awaiting_command = None
            if session.on_shell_ready is not None:
                await session.on_shell_ready(session.terminal_id)
            return


    async def _complete_active_command(
        self,
        session: _TerminalSession,
        command_id: str,
        *,
        exit_code: int | None,
        error_message: str | None,
        final_status: str = "completed",
    ) -> None:
        active = session.active_command
        if active is None or active.command_id != command_id or active.completed:
            return
        active.completed = True
        session.active_command = None
        session.awaiting_command = active
        if active.timeout_task is not None:
            get_runtime_task_registry().cancel(
                active.timeout_task,
                message="terminal command completed",
                diagnostic_context={"terminal_id": session.terminal_id, "command_id": command_id},
            )
            active.timeout_task = None
        if session.on_command_finished is not None:
            await session.on_command_finished(command_id, exit_code, error_message, final_status)
        else:
            await active.on_exit(exit_code, error_message)

    async def _fail_active_command(self, session: _TerminalSession, error_message: str) -> None:
        active = session.active_command
        if active is None or active.completed:
            return
        active.completed = True
        session.active_command = None
        session.awaiting_command = None
        if active.timeout_task is not None:
            get_runtime_task_registry().cancel(
                active.timeout_task,
                message="terminal command failed",
                diagnostic_context={"terminal_id": session.terminal_id, "command_id": active.command_id},
            )
        if session.on_command_finished is not None:
            await session.on_command_finished(active.command_id, None, error_message, "failed")
        else:
            await active.on_exit(None, error_message)

    def _arm_ready_watchdog(self, session: _TerminalSession, command_id: str) -> None:
        self._cancel_ready_watchdog(session, message="rearm ready watchdog")
        session.ready_watchdog_task = get_runtime_task_registry().create_task(
            self._ready_watchdog(session, command_id),
            task_name=f"terminal-ready-watchdog-{session.terminal_id}-{command_id}",
            owner_type="terminal",
            owner_id=session.terminal_id,
            task_class="ready_watchdog",
            diagnostic_context={"terminal_id": session.terminal_id, "command_id": command_id},
        )

    async def _notify_terminal_exit(
        self,
        session: _TerminalSession,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        if session.exit_notified:
            return
        session.exit_notified = True
        await session.on_exit(exit_code, error_message)

    def _cancel_ready_watchdog(self, session: _TerminalSession, *, message: str) -> None:
        task = session.ready_watchdog_task
        if task is None:
            return
        session.ready_watchdog_task = None
        current = asyncio.current_task()
        if task is current:
            # Watchdog is driving terminate; detach without cancelling self.
            return
        if not task.done():
            get_runtime_task_registry().cancel(
                task,
                message=message,
                diagnostic_context={"terminal_id": session.terminal_id},
            )

    async def _ready_watchdog(self, session: _TerminalSession, command_id: str) -> None:
        try:
            await asyncio.sleep(_SHELL_READY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        if session.closing or session.shell_ready:
            return
        # Command already finished at DONE; missing READY must not leave terminal permanently busy.
        logger.error(
            "Shell READY(B) missing after DONE: terminal_id=%s command_id=%s timeout_s=%s",
            session.terminal_id,
            command_id,
            _SHELL_READY_TIMEOUT_SECONDS,
        )
        session.awaiting_command = None
        # Detach before terminate so terminate_terminal does not cancel this task.
        if session.ready_watchdog_task is asyncio.current_task():
            session.ready_watchdog_task = None
        error_message = (
            f"Shell Integration READY(B) missing within {_SHELL_READY_TIMEOUT_SECONDS:.0f}s after DONE; "
            f"terminal_id={session.terminal_id} command_id={command_id}"
        )
        try:
            await self.terminate_terminal(session.terminal_id)
        except Exception:
            logger.exception(
                "Failed to terminate terminal after missing READY: terminal_id=%s command_id=%s",
                session.terminal_id,
                command_id,
            )
        # Exactly-once: only notify if watcher did not already fire on_exit during terminate.
        try:
            await self._notify_terminal_exit(session, None, error_message)
        except Exception:
            logger.exception(
                "Failed to publish terminal exit after missing READY: terminal_id=%s command_id=%s",
                session.terminal_id,
                command_id,
            )

    async def _enforce_timeout(self, session: _TerminalSession, command_id: str, timeout_ms: int) -> None:
        try:
            await asyncio.sleep(timeout_ms / 1000)
            active = session.active_command
            if active is None or active.command_id != command_id:
                return
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or f"terminal-timeout-{session.terminal_id}-{command_id}",
                diagnostic_context={"terminal_id": session.terminal_id, "command_id": command_id, "phase": "timeout_interrupt"},
            )
            await _write_terminal(session, "\x03", operation="terminal.timeout.interrupt.write")
            await asyncio.sleep(3)
            active = session.active_command
            if active is not None and active.command_id == command_id:
                await self._complete_active_command(
                    session,
                    command_id,
                    exit_code=None,
                    error_message=f"命令执行超时: {timeout_ms}ms",
                    final_status="timeout",
                )
                await _close_terminal_session(session, reason="command_timeout")
        except asyncio.CancelledError:
            raise

async def _start_unix_terminal(request: TerminalSessionStartRequest, *, on_exit: TerminalExitHandler) -> _TerminalSession:
    import fcntl
    import pty
    import struct
    import termios

    canonical_shell = resolve_supported_shell(request.shell)
    # openpty first so script temp files are not leaked if PTY allocation fails.
    master_fd, slave_fd = pty.openpty()
    temp_paths: list[str] = []
    process = None
    try:
        argv, env, temp_paths = _build_shell_launch(canonical_shell)
        rows = max(1, request.rows)
        cols = max(1, request.cols)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def preexec() -> None:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=request.cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=preexec,
            close_fds=True,
        )
    except BaseException:
        try:
            os.close(master_fd)
        except OSError:
            pass
        _cleanup_path_list(temp_paths)
        raise
    finally:
        try:
            os.close(slave_fd)
        except OSError:
            pass
    os.set_blocking(master_fd, False)
    return _TerminalSession(
        terminal_id=request.terminal_id,
        shell=canonical_shell,
        on_exit=on_exit,
        runtime_token=request.runtime_token,
        process=process,
        master_fd=master_fd,
        integration_paths=temp_paths,
    )


async def _start_windows_terminal(request: TerminalSessionStartRequest, *, on_exit: TerminalExitHandler) -> _TerminalSession:
    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise RuntimeError("Windows ConPTY 终端需要安装 pywinpty 依赖。") from exc
    canonical_shell = resolve_supported_shell(request.shell)
    argv, env, temp_paths = _build_shell_launch(canonical_shell)
    try:
        winpty = await _spawn_windows_pty_with_recovery(
            PtyProcess,
            shell=canonical_shell,
            cwd=request.cwd,
            argv=argv,
            env=env,
            dimensions=(max(1, request.rows), max(1, request.cols)),
        )
    except BaseException:
        _cleanup_path_list(temp_paths)
        raise
    return _TerminalSession(
        terminal_id=request.terminal_id,
        shell=canonical_shell,
        on_exit=on_exit,
        runtime_token=request.runtime_token,
        winpty=winpty,
        integration_paths=temp_paths,
    )


async def _spawn_windows_pty_with_recovery(
    pty_process: Any,
    *,
    shell: str,
    cwd: str,
    argv: list[str],
    env: dict[str, str],
    dimensions: tuple[int, int],
) -> Any:
    retried = False
    while True:
        try:
            return _spawn_windows_pty(pty_process, argv=argv, cwd=cwd, env=env, dimensions=dimensions)
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            if not retried and _is_transient_windows_pty_start_error(exc):
                retried = True
                await asyncio.sleep(0.05)
                continue
            raise _windows_pty_start_error(exc, shell=shell, cwd=cwd, argv=argv, retried=retried) from exc


def _spawn_windows_pty(
    pty_process: Any,
    *,
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    dimensions: tuple[int, int],
) -> Any:
    try:
        return pty_process.spawn(list(argv), cwd=cwd, env=env, dimensions=dimensions)
    except TypeError as exc:
        if not _is_pywinpty_dimensions_unsupported(exc):
            raise
        return pty_process.spawn(list(argv), cwd=cwd, env=env)


def _is_pywinpty_dimensions_unsupported(exc: TypeError) -> bool:
    text = str(exc).lower()
    return "dimensions" in text and (
        "unexpected keyword" in text
        or "got an unexpected" in text
        or "invalid keyword" in text
    )


def _is_transient_windows_pty_start_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__module__}.{exc.__class__.__name__}: {exc}".lower()
    has_native_panic = (
        "called `result::unwrap()`" in text
        or "pyo3_runtime.panicexception" in text
        or "panicexception" in text
    )
    has_success_hresult = (
        "hresult(0x00000000)" in text
        or "operation completed successfully" in text
        or "操作成功完成" in text
    )
    return has_native_panic and has_success_hresult


def _windows_pty_start_error(
    exc: BaseException,
    *,
    shell: str,
    cwd: str,
    argv: list[str],
    retried: bool,
) -> RuntimeError:
    detail = str(exc) or exc.__class__.__name__
    retry_note = "；已对可疑 native transient 错误重试 1 次" if retried else ""
    native_hint = ""
    if _is_transient_windows_pty_start_error(exc):
        native_hint = (
            "；检测到底层 pywinpty/winpty-rs/ConPTY 返回 Result::unwrap/HRESULT(0) 异常，"
            "这通常不是 cwd 不存在或 shell 无法解析，而是 Windows PTY native 层失败或状态竞争"
        )
    return RuntimeError(
        f"Windows PTY 终端启动失败{retry_note}: "
        f"shell={shell!r}, cwd={cwd!r}, argv={argv!r}, "
        f"error_type={exc.__class__.__module__}.{exc.__class__.__name__}, "
        f"error={detail}{native_hint}"
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


async def _write_terminal(session: _TerminalSession, text: str, *, operation: str = "terminal.write") -> None:
    payload_bytes = len(text.encode("utf-8", errors="replace"))
    if session.master_fd is not None:
        logger.debug(
            "Unix PTY write started: terminal_id=%s active_command_id=%s operation=%s payload_bytes=%s",
            session.terminal_id,
            _active_command_id(session),
            operation,
            payload_bytes,
        )
        os.write(session.master_fd, text.encode("utf-8", errors="replace"))
        logger.debug(
            "Unix PTY write completed: terminal_id=%s active_command_id=%s operation=%s payload_bytes=%s",
            session.terminal_id,
            _active_command_id(session),
            operation,
            payload_bytes,
        )
        return
    if session.winpty is not None:
        await _run_windows_pty_control(
            session,
            session.winpty.write,
            text,
            operation=operation,
            caller="_write_terminal",
            diagnostic_context={"payload_bytes": payload_bytes},
        )
        return
    raise KeyError(f"终端运行时不可写: {session.terminal_id}")


async def _close_terminal_session(session: _TerminalSession, *, reason: str = "close") -> None:
    close_started_at = time.perf_counter()
    session.closing = True
    logger.info(
        "PTY terminal close started: terminal_id=%s runtime_token=%s active_command_id=%s reason=%s has_process=%s has_winpty=%s",
        session.terminal_id,
        session.runtime_token,
        _active_command_id(session),
        reason,
        session.process is not None,
        session.winpty is not None,
    )
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
            await _run_windows_pty_control(
                session,
                session.winpty.close,
                True,
                operation="terminal.windows_pty.close",
                caller="_close_terminal_session",
                diagnostic_context={"reason": reason, "close_signature": "close_true"},
            )
        except TypeError:
            try:
                await _run_windows_pty_control(
                    session,
                    session.winpty.close,
                    operation="terminal.windows_pty.close",
                    caller="_close_terminal_session",
                    diagnostic_context={"reason": reason, "close_signature": "close_no_args"},
                )
            except BaseException as exc:
                if _is_process_control_exception(exc):
                    raise
                await _kill_windows_pty_after_close_failure(session, reason=reason, exc=exc)
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            await _kill_windows_pty_after_close_failure(session, reason=reason, exc=exc)
    if session.master_fd is not None:
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        session.master_fd = None
    # Process/PTY closed: safe to delete managed scripts/dirs with retry.
    _cleanup_integration_paths(session)
    logger.info(
        "PTY terminal close completed: terminal_id=%s runtime_token=%s reason=%s duration_ms=%s",
        session.terminal_id,
        session.runtime_token,
        reason,
        int((time.perf_counter() - close_started_at) * 1000),
    )


def _cleanup_integration_paths(session: _TerminalSession) -> None:
    if not session.integration_paths:
        return
    remaining = _cleanup_path_list(session.integration_paths)
    # Retry once after process teardown for Windows file locks.
    if remaining:
        remaining = _cleanup_path_list(remaining)
    session.integration_paths = remaining


def _cleanup_path_list(paths: list[str]) -> list[str]:
    # Reverse order so files inside temp dirs are removed before the dir itself when both listed.
    remaining: list[str] = []
    for path in reversed(list(paths)):
        if not path:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif not os.path.exists(path):
                continue
            else:
                remaining.append(path)
        except OSError:
            logger.debug("Failed to cleanup integration path: %s", path, exc_info=True)
            remaining.append(path)
    remaining.reverse()
    return remaining


def _is_definite_unwritten_error(exc: BaseException) -> bool:
    from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPoolOverloadedError

    return isinstance(exc, BlockingPoolOverloadedError)


async def _run_windows_pty_control(
    session: _TerminalSession,
    func: Any,
    /,
    *args: Any,
    operation: str,
    caller: str,
    diagnostic_context: dict[str, Any] | None = None,
    timeout_seconds: float = _WINDOWS_PTY_CONTROL_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> Any:
    command_id = _active_command_id(session)
    context = {
        "terminal_id": session.terminal_id,
        "runtime_token": session.runtime_token,
        "active_command_id": command_id,
        "command_id": command_id,
        "timeout_seconds": timeout_seconds,
    }
    if diagnostic_context:
        context.update(diagnostic_context)
    try:
        return await asyncio.wait_for(
            monitored_to_thread(
                func,
                *args,
                operation=operation,
                caller=caller,
                pool=BlockingPool.TERMINAL_CONTROL,
                log=logger,
                slow_queue_seconds=_WINDOWS_PTY_CONTROL_SLOW_QUEUE_SECONDS,
                diagnostic_context=context,
                **kwargs,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        logger.error(
            "Windows PTY control operation timed out: terminal_id=%s runtime_token=%s command_id=%s operation=%s timeout_seconds=%s",
            session.terminal_id,
            session.runtime_token,
            command_id,
            operation,
            timeout_seconds,
        )
        raise RuntimeError(
            f"Windows PTY control operation timed out: operation={operation} "
            f"terminal_id={session.terminal_id} timeout_seconds={timeout_seconds}"
        ) from exc
    except BaseException as exc:
        if _is_process_control_exception(exc):
            raise
        if isinstance(exc, TypeError):
            raise
        from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPoolOverloadedError

        if isinstance(exc, BlockingPoolOverloadedError):
            # Keep strong type so callers can prove write was never submitted.
            raise
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc


async def _kill_windows_pty_after_close_failure(
    session: _TerminalSession,
    *,
    reason: str,
    exc: BaseException,
    fallback: str = "close_failure",
) -> None:
    try:
        await _run_windows_pty_control(
            session,
            session.winpty.kill,
            operation="terminal.windows_pty.kill",
            caller="_close_terminal_session",
            diagnostic_context={
                "reason": reason,
                "fallback": fallback,
                "after_close_error_type": exc.__class__.__name__,
            },
        )
    except BaseException as kill_exc:
        if _is_process_control_exception(kill_exc):
            raise
        logger.warning(
            "Windows PTY kill after close failure failed: terminal_id=%s reason=%s fallback=%s error_type=%s error=%s",
            session.terminal_id,
            reason,
            fallback,
            kill_exc.__class__.__name__,
            str(kill_exc),
        )


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


def _active_command_id(session: _TerminalSession) -> str | None:
    active = session.active_command
    return active.command_id if active is not None else None


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


def _build_shell_launch(shell: str) -> tuple[list[str], dict[str, str], list[str]]:
    """Build argv/env/temp paths for the platform-supported integrated shell."""
    canonical = resolve_supported_shell(shell)
    executable = require_shell_executable(canonical)
    script_text = integration_script_for_shell(canonical)
    temp_paths: list[str] = []
    env = _process_env()

    if canonical == "pwsh":
        script_path = _write_temp_script(script_text, suffix=".ps1")
        temp_paths.append(script_path)
        argv = [executable, "-NoLogo", "-NoProfile", "-NoExit", "-File", script_path]
        return argv, env, temp_paths

    if canonical == "bash":
        script_path = _write_temp_script(script_text, suffix=".bash")
        temp_paths.append(script_path)
        argv = [executable, "--noprofile", "--rcfile", script_path, "-i"]
        return argv, env, temp_paths

    if canonical == "zsh":
        zdotdir = tempfile.mkdtemp(prefix="spread-ai-zsh-")
        temp_paths.append(zdotdir)
        zshrc_path = str(Path(zdotdir) / ".zshrc")
        Path(zshrc_path).write_text(script_text, encoding="utf-8", newline="\n")
        temp_paths.append(zshrc_path)
        env = dict(env)
        env["ZDOTDIR"] = zdotdir
        argv = [executable, "-i"]
        return argv, env, temp_paths

    raise RuntimeError(f"未实现 shell={canonical!r} 的 Shell Integration 启动方式。")


def _write_temp_script(script_text: str, *, suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        suffix=suffix,
        prefix="spread-ai-shell-integration-",
        delete=False,
    )
    with handle:
        handle.write(script_text)
    return handle.name


def _process_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform.startswith("win"):
        env.setdefault("PYTHONUTF8", "1")
        # winpty 在未设置该调试开关时默认隐藏 agent console；release GUI 进程不能继承
        # 用户环境中的 WINPTY_SHOW_CONSOLE=1，否则 Agent terminal_create 会短暂弹出黑框。
        env.pop("WINPTY_SHOW_CONSOLE", None)
    return env
