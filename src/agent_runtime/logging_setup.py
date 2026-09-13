from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any

_DEFAULT_RUNTIME_LOG_LEVEL = "DEBUG"
_RUNTIME_LOG_LEVEL_ENV = "SPREAD_AI_RUNTIME_LOG_LEVEL"
_BACKEND_ERROR_LOG_ENV = "SPREAD_AI_BACKEND_ERROR_LOG"
_DESKTOP_BACKEND_ERROR_LOG_ENV = "SPREAD_AI_DESKTOP_BACKEND_ERROR_LOG"
_DESKTOP_BACKEND_STDERR_LOG_ENV = "SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG"
_DATA_ROOT_ENV = "SPREAD_AI_DATA_ROOT"

_ERROR_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_ERROR_LOG_BACKUP_COUNT = 5

_installed_excepthooks = False
_excepthook_lock = threading.Lock()


def resolve_error_log_path() -> Path:
    """解析系统错误日志（backend-error.log）的标准存储路径。

    解析优先级：
    1. SPREAD_AI_BACKEND_ERROR_LOG 或 SPREAD_AI_DESKTOP_BACKEND_ERROR_LOG 显式配置；
    2. SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG 所在目录同级的 backend-error.log；
    3. ${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/logs/backend-error.log 兜底路径。
    """
    for env_var in (_BACKEND_ERROR_LOG_ENV, _DESKTOP_BACKEND_ERROR_LOG_ENV):
        val = os.getenv(env_var)
        if val and val.strip():
            return Path(val.strip()).resolve()

    stderr_env = os.getenv(_DESKTOP_BACKEND_STDERR_LOG_ENV)
    if stderr_env and stderr_env.strip():
        return (Path(stderr_env.strip()).resolve().parent / "backend-error.log").resolve()

    data_root = os.getenv(_DATA_ROOT_ENV, "~/.spread-ai")
    return (Path(data_root).expanduser().resolve() / "logs" / "backend-error.log").resolve()


def get_runtime_log_level() -> int:
    """获取后端通用运行日志级别（默认 DEBUG）。"""
    configured_level = os.getenv(_RUNTIME_LOG_LEVEL_ENV, _DEFAULT_RUNTIME_LOG_LEVEL).strip().upper()
    level = logging.getLevelName(configured_level)
    if isinstance(level, int):
        return level
    return logging.DEBUG


def install_unhandled_exception_hooks(logger: logging.Logger | None = None) -> None:
    """全局注册未捕获异常钩子，确保任意未捕获的全局崩溃或线程异常均被记录到 ERROR 日志。"""
    global _installed_excepthooks
    with _excepthook_lock:
        if _installed_excepthooks:
            return
        _installed_excepthooks = True

    target_logger = logger or logging.getLogger("agent_runtime.system")
    original_excepthook = sys.excepthook

    def _sys_excepthook(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            original_excepthook(exc_type, exc_value, exc_traceback)
            return
        target_logger.critical(
            "未捕获的系统全局异常 (sys.excepthook): %s",
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        original_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        original_thread_excepthook = threading.excepthook

        def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
            if issubclass(args.exc_type, KeyboardInterrupt):
                original_thread_excepthook(args)
                return
            thread_name = args.thread.name if args.thread else "unknown"
            target_logger.critical(
                "未捕获的线程异常 (threading.excepthook: thread=%s): %s",
                thread_name,
                args.exc_value,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            original_thread_excepthook(args)

        threading.excepthook = _threading_excepthook


def setup_runtime_logging(
    *,
    log_level: int | None = None,
    error_log_path: str | Path | None = None,
    max_bytes: int = _ERROR_LOG_MAX_BYTES,
    backup_count: int = _ERROR_LOG_BACKUP_COUNT,
) -> tuple[logging.Logger, RotatingFileHandler]:
    """统一配置全局 Logging 系统并挂载专用的 backend-error.log 错误日志 Handler。

    特性：
    1. 保留原有 stdout/stderr/控制台全量流式日志（包含 DEBUG、INFO、WARNING、ERROR）；
    2. 单独创建 RotatingFileHandler，过滤级别严格限定为 ERROR 及以上；
    3. 专职收集全部 logger.error、logger.exception、logger.critical 与未捕获异常；
    4. 自动创建日志目录并配置 10MB / 5 个备份的日志轮转；
    5. 自动配置 uvicorn 相关 logger 确保网络层 500 异常一并汇入错误日志。
    """
    effective_log_level = log_level if log_level is not None else get_runtime_log_level()

    # 1. 基础 Logging 初始化 (全量日志输出至 stderr)
    logging.basicConfig(
        level=effective_log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(effective_log_level)
    for handler in root_logger.handlers:
        handler.setLevel(effective_log_level)

    # 2. 专用系统错误日志 Handler 初始化
    resolved_error_path = Path(error_log_path).resolve() if error_log_path else resolve_error_log_path()
    resolved_error_path.parent.mkdir(parents=True, exist_ok=True)

    # 移除可能已存在的相同文件 Handler，避免重复添加
    for existing in list(root_logger.handlers):
        if isinstance(existing, RotatingFileHandler):
            try:
                if Path(existing.baseFilename).resolve() == resolved_error_path:
                    root_logger.removeHandler(existing)
                    existing.close()
            except Exception:
                pass

    error_handler = RotatingFileHandler(
        str(resolved_error_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [pid:%(process)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(error_handler)

    # 3. 联动 uvicorn 异常日志
    for uvicorn_name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_name)
        uv_logger.propagate = True

    # 4. 注册未捕获全局异常钩子
    install_unhandled_exception_hooks(root_logger)

    return root_logger, error_handler
