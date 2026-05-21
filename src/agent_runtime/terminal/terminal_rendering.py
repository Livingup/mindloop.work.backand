from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.terminal.terminal_text_projection import render_terminal_text

_DEFAULT_COLUMNS = 120
_DEFAULT_ROWS = 30
_DEFAULT_SCROLLBACK = 10000
_XTERM_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class TerminalRenderedText:
    text: str
    line_count: int
    renderer: str


def render_terminal_chunks(
    chunks: list[str],
    *,
    columns: int = _DEFAULT_COLUMNS,
    rows: int = _DEFAULT_ROWS,
    scrollback: int = _DEFAULT_SCROLLBACK,
) -> TerminalRenderedText:
    normalized_chunks = [chunk for chunk in chunks if chunk]
    if not normalized_chunks:
        return TerminalRenderedText(text="", line_count=0, renderer="empty")
    xterm_rendered = _render_with_xterm(normalized_chunks, columns=columns, rows=rows, scrollback=scrollback)
    if xterm_rendered is not None:
        return xterm_rendered
    text = render_terminal_text("".join(normalized_chunks), columns=columns, rows=rows, scrollback_limit=scrollback)
    return TerminalRenderedText(text=text, line_count=_line_count(text), renderer="python-projector")


def _render_with_xterm(chunks: list[str], *, columns: int, rows: int, scrollback: int) -> TerminalRenderedText | None:
    node = shutil.which("node")
    if not node:
        return None
    script_path = Path(__file__).resolve().with_name("terminal_xterm_snapshot.js")
    if not script_path.exists():
        return None
    cwd = _find_xterm_working_directory()
    if cwd is None:
        return None
    payload = json.dumps(
        {
            "chunks": chunks,
            "columns": columns,
            "rows": rows,
            "scrollback": scrollback,
        },
        ensure_ascii=False,
    )
    try:
        completed = subprocess.run(
            [node, str(script_path)],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=str(cwd),
            timeout=_XTERM_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    text = str(output.get("text", ""))
    line_count = output.get("line_count")
    return TerminalRenderedText(
        text=text,
        line_count=int(line_count) if isinstance(line_count, int) else _line_count(text),
        renderer="xterm",
    )


def _find_xterm_working_directory() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "node_modules" / "@xterm" / "xterm"
        if candidate.exists():
            return parent
        sibling = parent / "agent-frontend" / "node_modules" / "@xterm" / "xterm"
        if sibling.exists():
            return parent / "agent-frontend"
    return None


def _line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0
