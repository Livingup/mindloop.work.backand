from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_runtime import desktop_server


def test_install_rotating_standard_error_rotates_when_size_exceeds_limit(tmp_path: Path) -> None:
    log_path = tmp_path / "backend-stderr.log"
    original_stderr = sys.stderr
    original_dunder_stderr = sys.__stderr__
    original_fd = os.dup(2)

    try:
        stream = desktop_server._install_rotating_standard_error(
            log_path,
            max_bytes=128,
            backup_count=2,
            detach_inherited_fd=False,
        )

        stream.write("a" * 120)
        stream.write("b" * 20)
        stream.write("c" * 10)
        stream.flush()

        assert sys.stderr is stream
        assert sys.__stderr__ is stream
        assert log_path.exists()
        assert log_path.with_name("backend-stderr.log.1").exists()
        assert log_path.read_text(encoding="utf-8") == "b" * 20 + "c" * 10
        assert log_path.with_name("backend-stderr.log.1").read_text(encoding="utf-8") == "a" * 120

        stream.close()
    finally:
        if isinstance(sys.stderr, desktop_server._RotatingTextStream) and not sys.stderr.closed:
            sys.stderr.close()
        sys.stderr = original_stderr
        sys.__stderr__ = original_dunder_stderr
        os.dup2(original_fd, 2)
        os.close(original_fd)
