from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalConfig:
    data_dir: str = "~/.spread-ai/terminal"
    default_shell: str | None = None
    max_output_chars_per_read: int = 50000

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("terminal.data_dir 不能为空。")
        if self.default_shell is not None and not self.default_shell.strip():
            raise ValueError("terminal.default_shell 不能为空白字符串。")
        if self.max_output_chars_per_read < 1000:
            raise ValueError("terminal.max_output_chars_per_read 不能小于 1000。")
