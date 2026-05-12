from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeAlias

from agent_runtime.terminal.terminal import require_non_blank, utc_now

TerminalOutputStream: TypeAlias = str

TERMINAL_OUTPUT_STREAM_STDOUT: Final[str] = "stdout"
TERMINAL_OUTPUT_STREAM_STDERR: Final[str] = "stderr"
TERMINAL_OUTPUT_STREAM_PTY: Final[str] = "pty"

TERMINAL_OUTPUT_STREAMS: Final[frozenset[str]] = frozenset({
    TERMINAL_OUTPUT_STREAM_STDOUT,
    TERMINAL_OUTPUT_STREAM_STDERR,
    TERMINAL_OUTPUT_STREAM_PTY,
})


@dataclass(frozen=True, slots=True)
class TerminalOutputChunk:
    terminal_id: str
    command_id: str
    chunk_index: int
    byte_offset: int
    stream: TerminalOutputStream
    chunk: str
    encoding: str = "utf-8"
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.terminal_id, "terminal_id")
        require_non_blank(self.command_id, "command_id")
        require_non_blank(self.encoding, "encoding")
        if not isinstance(self.chunk, str) or self.chunk == "":
            raise ValueError("chunk 不能为空字符串。")
        if self.chunk_index < 0:
            raise ValueError("chunk_index 不能为负数。")
        if self.byte_offset < 0:
            raise ValueError("byte_offset 不能为负数。")
        if self.stream not in TERMINAL_OUTPUT_STREAMS:
            raise ValueError(f"未知终端输出流: {self.stream}")
        if self.created_at is None:
            object.__setattr__(self, "created_at", utc_now())
