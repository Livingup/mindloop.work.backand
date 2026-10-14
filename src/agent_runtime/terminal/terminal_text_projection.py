from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

_DEFAULT_COLUMNS: Final[int] = 120
_DEFAULT_ROWS: Final[int] = 30
_DEFAULT_SCROLLBACK_LIMIT: Final[int] = 10000
_CSI_FINAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x40-\x7e]")


def render_terminal_text(
    raw_text: str,
    *,
    columns: int = _DEFAULT_COLUMNS,
    rows: int = _DEFAULT_ROWS,
    scrollback_limit: int = _DEFAULT_SCROLLBACK_LIMIT,
) -> str:
    if not raw_text:
        return ""
    projector = TerminalTextProjector(columns=columns, rows=rows, scrollback_limit=scrollback_limit)
    projector.write(raw_text)
    return projector.text()


class TerminalTextProjector:
    def __init__(
        self,
        *,
        columns: int = _DEFAULT_COLUMNS,
        rows: int = _DEFAULT_ROWS,
        scrollback_limit: int = _DEFAULT_SCROLLBACK_LIMIT,
    ) -> None:
        self._renderer = _TerminalTextRenderer(
            columns=max(1, columns),
            rows=max(1, rows),
            scrollback_limit=max(0, scrollback_limit),
        )

    def write(self, text: str) -> None:
        if text:
            self._renderer.write(text)

    def text(self) -> str:
        return self._renderer.text()

    def tail_text(self, tail_lines: int) -> str:
        text = self.text()
        if not text:
            return ""
        lines = text.splitlines(keepends=True)
        return "".join(lines[-max(1, tail_lines):])

    def line_count(self) -> int:
        text = self.text()
        return len(text.splitlines()) if text else 0


@dataclass(slots=True)
class _TerminalTextRenderer:
    columns: int
    rows: int
    scrollback_limit: int
    screen: list[list[str]] = field(init=False)
    scrollback: list[str] = field(default_factory=list)
    row: int = 0
    col: int = 0
    saved_cursor: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        self.screen = [[] for _ in range(self.rows)]

    def write(self, text: str) -> None:
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\x1b":
                index = self._consume_escape(text, index)
                continue
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self._linefeed()
                self.col = 0
            elif char == "\b":
                self.col = max(0, self.col - 1)
            elif char == "\t":
                self._write_tab()
            elif char == "\f":
                self._clear_visible()
            elif char >= " " and char != "\x7f":
                self._put_char(char)
            index += 1

    def text(self) -> str:
        lines = [*self.scrollback, *(_line_to_text(line) for line in self.screen)]
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    def _consume_escape(self, text: str, index: int) -> int:
        next_index = index + 1
        if next_index >= len(text):
            return len(text)
        marker = text[next_index]
        if marker == "[":
            return self._consume_csi(text, next_index + 1)
        if marker == "]":
            return _consume_until_string_terminator(text, next_index + 1, allow_bel=True)
        if marker in {"P", "^", "_", "X"}:
            return _consume_until_string_terminator(text, next_index + 1, allow_bel=False)
        if marker in {"7", "s"}:
            self.saved_cursor = (self.row, self.col)
            return next_index + 1
        if marker in {"8", "u"}:
            self._restore_cursor()
            return next_index + 1
        if marker == "c":
            self.scrollback.clear()
            self._clear_visible()
            return next_index + 1
        if marker in {"(", ")", "*", "+", "-", ".", "/"}:
            return min(len(text), next_index + 2)
        return next_index + 1

    def _consume_csi(self, text: str, index: int) -> int:
        match = _CSI_FINAL_PATTERN.search(text, index)
        if match is None:
            return len(text)
        final = match.group(0)
        params = text[index:match.start()]
        self._handle_csi(final, params)
        return match.end()

    def _handle_csi(self, final: str, params: str) -> None:
        values = _parse_csi_values(params)
        if final in {"m", "h", "l", "n", "q"}:
            return
        if final in {"H", "f"}:
            self._move_cursor(_csi_value(values, 0, 1) - 1, _csi_value(values, 1, 1) - 1)
        elif final == "A":
            self._move_cursor(self.row - _csi_value(values, 0, 1), self.col)
        elif final == "B":
            self._move_cursor(self.row + _csi_value(values, 0, 1), self.col)
        elif final == "C":
            self._move_cursor(self.row, self.col + _csi_value(values, 0, 1))
        elif final == "D":
            self._move_cursor(self.row, self.col - _csi_value(values, 0, 1))
        elif final == "E":
            self._move_cursor(self.row + _csi_value(values, 0, 1), 0)
        elif final == "F":
            self._move_cursor(self.row - _csi_value(values, 0, 1), 0)
        elif final == "G":
            self._move_cursor(self.row, _csi_value(values, 0, 1) - 1)
        elif final == "d":
            self._move_cursor(_csi_value(values, 0, 1) - 1, self.col)
        elif final == "J":
            self._erase_display(_csi_value(values, 0, 0))
        elif final == "K":
            self._erase_line(_csi_value(values, 0, 0))
        elif final == "X":
            self._erase_characters(_csi_value(values, 0, 1))
        elif final == "P":
            self._delete_characters(_csi_value(values, 0, 1))
        elif final == "@":
            self._insert_blank_characters(_csi_value(values, 0, 1))
        elif final == "S":
            self._scroll_up(_csi_value(values, 0, 1))
        elif final == "T":
            self._scroll_down(_csi_value(values, 0, 1))
        elif final == "s":
            self.saved_cursor = (self.row, self.col)
        elif final == "u":
            self._restore_cursor()

    def _put_char(self, char: str) -> None:
        if self.col >= self.columns:
            self._linefeed()
            self.col = 0
        line = self.screen[self.row]
        _ensure_line_length(line, self.col)
        line[self.col] = char
        self.col += 1

    def _write_tab(self) -> None:
        spaces = 8 - (self.col % 8)
        for _ in range(spaces):
            self._put_char(" ")

    def _linefeed(self) -> None:
        self.row += 1
        if self.row < self.rows:
            return
        self._scroll_up(1)
        self.row = self.rows - 1

    def _scroll_up(self, count: int) -> None:
        for _ in range(max(0, count)):
            self._append_scrollback(self.screen.pop(0))
            self.screen.append([])

    def _scroll_down(self, count: int) -> None:
        for _ in range(max(0, count)):
            self.screen.insert(0, [])
            self.screen.pop()

    def _append_scrollback(self, line: list[str]) -> None:
        if self.scrollback_limit == 0:
            return
        self.scrollback.append(_line_to_text(line))
        if len(self.scrollback) > self.scrollback_limit:
            del self.scrollback[: len(self.scrollback) - self.scrollback_limit]

    def _move_cursor(self, row: int, col: int) -> None:
        self.row = min(max(row, 0), self.rows - 1)
        self.col = min(max(col, 0), max(0, self.columns - 1))

    def _restore_cursor(self) -> None:
        if self.saved_cursor is None:
            return
        self._move_cursor(self.saved_cursor[0], self.saved_cursor[1])

    def _erase_line(self, mode: int) -> None:
        line = self.screen[self.row]
        if mode == 1:
            _ensure_line_length(line, self.col)
            for index in range(self.col + 1):
                line[index] = " "
        elif mode == 2:
            line.clear()
        else:
            if self.col < len(line):
                del line[self.col:]

    def _erase_display(self, mode: int) -> None:
        if mode == 1:
            for row_index in range(self.row):
                self.screen[row_index].clear()
            self._erase_line(1)
        elif mode in {2, 3}:
            if mode == 3:
                self.scrollback.clear()
            self._clear_visible()
        else:
            self._erase_line(0)
            for row_index in range(self.row + 1, self.rows):
                self.screen[row_index].clear()

    def _clear_visible(self) -> None:
        self.screen = [[] for _ in range(self.rows)]
        self.row = 0
        self.col = 0

    def _erase_characters(self, count: int) -> None:
        line = self.screen[self.row]
        if self.col >= len(line):
            return
        end = min(len(line), self.col + max(0, count))
        for index in range(self.col, end):
            line[index] = " "

    def _delete_characters(self, count: int) -> None:
        line = self.screen[self.row]
        if self.col >= len(line):
            return
        del line[self.col:self.col + max(0, count)]

    def _insert_blank_characters(self, count: int) -> None:
        line = self.screen[self.row]
        _ensure_line_length(line, self.col)
        for _ in range(max(0, count)):
            line.insert(self.col, " ")
        del line[self.columns:]


def _parse_csi_values(params: str) -> list[int | None]:
    cleaned = params.lstrip("?<>=")
    if not cleaned:
        return []
    values: list[int | None] = []
    for part in re.split(r"[;:]", cleaned):
        if part == "":
            values.append(None)
            continue
        match = re.search(r"-?\d+", part)
        values.append(int(match.group(0)) if match is not None else None)
    return values


def _csi_value(values: list[int | None], index: int, default: int) -> int:
    if index >= len(values):
        return default
    value = values[index]
    if value is None or value <= 0:
        return default
    return value


def _consume_until_string_terminator(text: str, index: int, *, allow_bel: bool) -> int:
    while index < len(text):
        if allow_bel and text[index] == "\x07":
            return index + 1
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def _ensure_line_length(line: list[str], index: int) -> None:
    if index >= len(line):
        line.extend(" " for _ in range(index - len(line) + 1))


def _line_to_text(line: list[str]) -> str:
    return "".join(line).rstrip()
