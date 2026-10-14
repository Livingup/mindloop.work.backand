from __future__ import annotations

import logging
import os
from pathlib import Path
import sys

from agent_runtime.logging_setup import (
    install_unhandled_exception_hooks,
    resolve_error_log_path,
    setup_runtime_logging,
)


def test_resolve_error_log_path_explicit_env(monkeypatch) -> None:
    custom_path = Path("/tmp/custom_error.log").resolve()
    monkeypatch.setenv("SPREAD_AI_BACKEND_ERROR_LOG", str(custom_path))
    assert resolve_error_log_path() == custom_path


def test_resolve_error_log_path_from_stderr_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPREAD_AI_BACKEND_ERROR_LOG", raising=False)
    monkeypatch.delenv("SPREAD_AI_DESKTOP_BACKEND_ERROR_LOG", raising=False)
    stderr_path = tmp_path / "logs" / "backend-stderr.log"
    monkeypatch.setenv("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG", str(stderr_path))
    assert resolve_error_log_path() == (tmp_path / "logs" / "backend-error.log").resolve()


def test_resolve_error_log_path_from_data_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPREAD_AI_BACKEND_ERROR_LOG", raising=False)
    monkeypatch.delenv("SPREAD_AI_DESKTOP_BACKEND_ERROR_LOG", raising=False)
    monkeypatch.delenv("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG", raising=False)
    monkeypatch.setenv("SPREAD_AI_DATA_ROOT", str(tmp_path))
    assert resolve_error_log_path() == (tmp_path / "logs" / "backend-error.log").resolve()


def test_setup_runtime_logging_records_only_errors_and_above(tmp_path: Path) -> None:
    error_log_file = tmp_path / "backend-error.log"
    root_logger, handler = setup_runtime_logging(
        log_level=logging.DEBUG,
        error_log_path=error_log_file,
    )
    try:
        test_logger = logging.getLogger("agent_runtime.test_logger")

        # DEBUG, INFO, WARNING should not appear in error_log_file
        test_logger.debug("debug message 123")
        test_logger.info("info message 456")
        test_logger.warning("warning message 789")

        # Flush handler
        handler.flush()
        content = error_log_file.read_text(encoding="utf-8")
        assert "debug message 123" not in content
        assert "info message 456" not in content
        assert "warning message 789" not in content

        # ERROR and CRITICAL should appear
        test_logger.error("system error happened!")
        test_logger.critical("fatal crash occurred!")
        try:
            raise ValueError("explicit test exception")
        except ValueError:
            test_logger.exception("exception caught in task")

        handler.flush()
        content = error_log_file.read_text(encoding="utf-8")
        assert "ERROR [agent_runtime.test_logger]" in content
        assert "system error happened!" in content
        assert "CRITICAL [agent_runtime.test_logger]" in content
        assert "fatal crash occurred!" in content
        assert "exception caught in task" in content
        assert "ValueError: explicit test exception" in content

    finally:
        root_logger.removeHandler(handler)
        handler.close()


def test_setup_runtime_logging_avoids_duplicate_handlers(tmp_path: Path) -> None:
    error_log_file = tmp_path / "backend-error.log"
    root_logger, handler1 = setup_runtime_logging(error_log_path=error_log_file)
    root_logger, handler2 = setup_runtime_logging(error_log_path=error_log_file)

    try:
        matching = [
            h for h in root_logger.handlers
            if getattr(h, "baseFilename", None) == str(error_log_file.resolve())
        ]
        assert len(matching) == 1

        test_logger = logging.getLogger("agent_runtime.dup_test")
        test_logger.error("single error log")
        handler2.flush()

        lines = [line for line in error_log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 1
        assert "single error log" in lines[0]
    finally:
        root_logger.removeHandler(handler2)
        handler2.close()
