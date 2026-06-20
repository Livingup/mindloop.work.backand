from __future__ import annotations

import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click

from agent_runtime.server import run_server


_BACKEND_STDERR_LOG_MAX_BYTES = 5 * 1024 * 1024
_BACKEND_STDERR_LOG_BACKUP_COUNT = 5
_STDERR_FILENO = 2


class _RotatingTextStream:
    """Small file-like stderr stream backed by RotatingFileHandler.

    Electron packaged mode starts the backend sidecar with stderr already attached to
    backend-stderr.log. Replacing sys.stderr with this stream keeps Python logging on
    the same file path while enforcing bounded 5MB rotation inside the backend process.
    """

    encoding = "utf-8"
    errors = "backslashreplace"

    def __init__(
        self,
        log_path: str | Path,
        *,
        max_bytes: int = _BACKEND_STDERR_LOG_MAX_BYTES,
        backup_count: int = _BACKEND_STDERR_LOG_BACKUP_COUNT,
    ) -> None:
        self.name = str(Path(log_path))
        Path(self.name).parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self.name,
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=self.encoding,
            delay=False,
        )
        self._lock = threading.RLock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, message: str) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed stderr log stream.")
        if not message:
            return 0
        text = str(message)
        with self._lock:
            stream = self._handler.stream
            if stream is None:
                stream = self._handler._open()
                self._handler.stream = stream
            try:
                stream.seek(0, os.SEEK_END)
                encoded_size = len(text.encode(self.encoding, errors="replace"))
                if self._handler.maxBytes > 0 and stream.tell() + encoded_size >= self._handler.maxBytes:
                    self._handler.doRollover()
                    stream = self._handler.stream
                    if stream is None:
                        stream = self._handler._open()
                        self._handler.stream = stream
            except OSError:
                # Keep stderr usable even if the platform temporarily refuses rollover.
                pass
            if stream is None:
                stream = self._handler._open()
                self._handler.stream = stream
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        if not self._closed:
            self._handler.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._handler.close()
        self._closed = True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        stream = self._handler.stream
        if stream is None:
            stream = self._handler._open()
            self._handler.stream = stream
        return stream.fileno()


def _open_fallback_stream(env_name: str):
    log_path = os.environ.get(env_name)
    if log_path:
        try:
            return open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            pass
    return open(os.devnull, "w", encoding="utf-8")


def _flush_stream_safely(stream) -> None:
    if stream is None or isinstance(stream, _RotatingTextStream):
        return
    try:
        stream.flush()
    except Exception:
        pass


def _detach_inherited_standard_error_handle() -> None:
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, _STDERR_FILENO)
        finally:
            os.close(devnull_fd)
    except OSError:
        pass


def _install_rotating_standard_error(
    log_path: str | Path,
    *,
    max_bytes: int = _BACKEND_STDERR_LOG_MAX_BYTES,
    backup_count: int = _BACKEND_STDERR_LOG_BACKUP_COUNT,
    detach_inherited_fd: bool = True,
) -> _RotatingTextStream:
    resolved_path = Path(log_path).resolve()
    current_stderr = sys.stderr
    if isinstance(current_stderr, _RotatingTextStream) and Path(current_stderr.name).resolve() == resolved_path:
        return current_stderr

    rotating_stream = _RotatingTextStream(resolved_path, max_bytes=max_bytes, backup_count=backup_count)
    previous_streams = {id(stream): stream for stream in (sys.stderr, getattr(sys, "__stderr__", None))}
    for stream in previous_streams.values():
        _flush_stream_safely(stream)
    sys.stderr = rotating_stream
    sys.__stderr__ = rotating_stream
    if detach_inherited_fd:
        _detach_inherited_standard_error_handle()
    return rotating_stream


def _ensure_standard_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _open_fallback_stream("SPREAD_AI_DESKTOP_BACKEND_STDOUT_LOG")

    stderr_log_path = os.environ.get("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG")
    if stderr_log_path:
        try:
            _install_rotating_standard_error(stderr_log_path)
            return
        except OSError:
            pass

    if sys.stderr is None:
        fallback_stream = _open_fallback_stream("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG")
        sys.stderr = fallback_stream
        sys.__stderr__ = fallback_stream


_ensure_standard_streams()


@click.command()
@click.option(
    "--config-file",
    "config_file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="正式桌面版内部后端使用的 agent_runtime.yaml 配置文件路径。",
)
@click.option(
    "--env-file",
    "env_file_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="开发/诊断场景显式传入的 env 覆盖文件；正式 release 启动不使用。",
)
@click.option(
    "--frontend-dist-dir",
    "frontend_dist_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="正式 release 浏览器/移动端 HTTP 前端的 Vite dist 目录；不传则只提供 Runtime API。",
)
@click.option("--host", default=None, help="开发/诊断场景监听地址覆盖；正式 release 启动不传该参数。")
@click.option("--port", default=None, type=int, help="开发/诊断场景监听端口覆盖；正式 release 启动不传该参数。")
def main(
    config_file_path: Path,
    env_file_path: Path | None,
    frontend_dist_dir: Path | None,
    host: str | None,
    port: int | None,
) -> None:
    run_server(
        config_file_path=config_file_path,
        env_file_path=env_file_path,
        host=host,
        port=port,
        watch=False,
        frontend_dist_dir=frontend_dist_dir,
    )


if __name__ == "__main__":
    main()
