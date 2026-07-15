from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalConfig:
    data_dir: str = "~/.spread-ai/terminal"
    default_shell: str | None = None
    max_output_chars_per_read: int = 50000
    # Runtime 同时 live terminal（已挂载 PTY/ConPTY runtime）硬上限。
    max_live_terminals: int = 200
    # 同一 agent_instance_id 同时 live terminal 上限。
    max_live_terminals_per_agent: int = 16
    # 输出 coalesce 时间窗（毫秒）；用于降低 SQLite/SSE 写放大。
    output_coalesce_interval_ms: int = 33
    # 单 terminal 待合并输出达到该字节数时立即 flush。
    output_coalesce_max_bytes: int = 8192

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("terminal.data_dir 不能为空。")
        if self.default_shell is not None and not self.default_shell.strip():
            raise ValueError("terminal.default_shell 不能为空白字符串。")
        if self.max_output_chars_per_read < 1000:
            raise ValueError("terminal.max_output_chars_per_read 不能小于 1000。")
        if self.max_live_terminals < 1:
            raise ValueError("terminal.max_live_terminals 不能小于 1。")
        if self.max_live_terminals_per_agent < 1:
            raise ValueError("terminal.max_live_terminals_per_agent 不能小于 1。")
        if self.max_live_terminals_per_agent > self.max_live_terminals:
            raise ValueError("terminal.max_live_terminals_per_agent 不能大于 max_live_terminals。")
        if self.output_coalesce_interval_ms < 0:
            raise ValueError("terminal.output_coalesce_interval_ms 不能为负数。")
        if self.output_coalesce_max_bytes < 1:
            raise ValueError("terminal.output_coalesce_max_bytes 不能小于 1。")
