from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from agent_runtime.terminal.shell_policy import SHELL_BASH, SHELL_POWERSHELL, SHELL_ZSH


class TokenKind(str, Enum):
    VISIBLE = "visible"
    DONE = "done"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class StreamToken:
    kind: TokenKind
    text: str = ""
    exit_code: int | None = None
    exit_code_present: bool = False


_OSC_RE: Final[re.Pattern[str]] = re.compile(
    r"\x1b\]633;SAI;(D(?:;[^\x07\x1b]*)?|B)(?:\x07|\x1b\\)"
)



class OrderedSaiStreamParser:
    """Parse PTY text into ordered tokens: visible / DONE / READY."""

    def __init__(self, *, max_buffer_chars: int = 8192) -> None:
        self._buffer = ""
        self._max_buffer_chars = max(256, max_buffer_chars)

    def feed(self, text: str) -> list[StreamToken]:
        if not text:
            return []
        self._buffer += text
        tokens: list[StreamToken] = []
        if len(self._buffer) > self._max_buffer_chars:
            overflow = self._buffer[: len(self._buffer) - self._max_buffer_chars]
            self._buffer = self._buffer[len(self._buffer) - self._max_buffer_chars :]
            tokens.append(StreamToken(kind=TokenKind.VISIBLE, text=overflow))
        tokens.extend(self._drain(final=False))
        return tokens

    def flush(self) -> list[StreamToken]:
        tokens = list(self._drain(final=True))
        if self._buffer:
            tokens.append(StreamToken(kind=TokenKind.VISIBLE, text=self._buffer))
            self._buffer = ""
        return tokens

    def _drain(self, *, final: bool) -> list[StreamToken]:
        tokens: list[StreamToken] = []
        while True:
            match = _OSC_RE.search(self._buffer)
            if match is None:
                if final:
                    return tokens
                hold = _partial_hold_length(self._buffer)
                if hold:
                    visible = self._buffer[:-hold]
                    self._buffer = self._buffer[-hold:]
                else:
                    visible = self._buffer
                    self._buffer = ""
                if visible:
                    tokens.append(StreamToken(kind=TokenKind.VISIBLE, text=visible))
                return tokens
            start, end = match.span()
            if start:
                tokens.append(StreamToken(kind=TokenKind.VISIBLE, text=self._buffer[:start]))
            body = match.group(1)
            if body == "B":
                tokens.append(StreamToken(kind=TokenKind.READY))
            elif body.startswith("D"):
                tokens.append(_done_token(body))
            self._buffer = self._buffer[end:]


def _done_token(body: str) -> StreamToken:
    if body in {"D", "D;"}:
        return StreamToken(kind=TokenKind.DONE, exit_code=None, exit_code_present=False)
    status = body[2:] if body.startswith("D;") else ""
    if status == "":
        return StreamToken(kind=TokenKind.DONE, exit_code=None, exit_code_present=False)
    try:
        return StreamToken(kind=TokenKind.DONE, exit_code=int(status), exit_code_present=True)
    except ValueError:
        return StreamToken(kind=TokenKind.DONE, exit_code=None, exit_code_present=False)


def _partial_hold_length(buffer: str) -> int:
    marker = "\x1b]633;SAI;"
    for n in range(min(len(buffer), len(marker)), 0, -1):
        if marker.startswith(buffer[-n:]):
            return n
    idx = buffer.rfind("\x1b]633;SAI;")
    if idx >= 0:
        tail = buffer[idx:]
        if "\x07" not in tail and "\x1b\\" not in tail:
            return len(buffer) - idx
    if buffer.endswith("\x1b"):
        return 1
    if buffer.endswith("\x1b]"):
        return 2
    return 0


def build_powershell_integration_script() -> str:
    # Reject non-5.1 engines before installing Prompt/READY hooks.
    return (
        "if (-not $PSVersionTable -or [int]$PSVersionTable.PSVersion.Major -ne 5 "
        "-or [int]$PSVersionTable.PSVersion.Minor -ne 1) {\n"
        "  $version = if ($PSVersionTable) { [string]$PSVersionTable.PSVersion } else { 'unknown' }\n"
        "  [Console]::Error.WriteLine(\"Windows PowerShell 5.1 is required; found version=$version\")\n"
        "  exit 1\n"
        "}\n"
        "$ErrorActionPreference = 'Continue'\n"
        "function Global:__SAI_Emit([string]$Body) {\n"
        "  [Console]::Write(([char]27) + ']633;SAI;' + $Body + ([char]7))\n"
        "}\n"
        "function Global:Prompt {\n"
        "  $__sai_ok = $?\n"
        "  $__sai_code = 0\n"
        "  if (-not $__sai_ok) { $__sai_code = 1 }\n"
        "  __SAI_Emit (\"D;\" + $__sai_code)\n"
        "  $location = $executionContext.SessionState.Path.CurrentLocation.Path\n"
        "  $suffix = '>' * ([Math]::Max(1, $nestedPromptLevel + 1))\n"
        "  $promptText = \"PS $location$suffix \"\n"
        "  [Console]::Out.Write($promptText)\n"
        "  __SAI_Emit 'B'\n"
        "  return ''\n"
        "}\n"
    )


def build_bash_integration_script() -> str:
    # READY is literal in PS1 with readline zero-width markers.
    return (
        "__sai_emit() { printf '\\033]633;SAI;%s\\007' \"$1\"; }\n"
        "__sai_prompt_command() {\n"
        "  local __sai_ec=$?\n"
        "  __sai_emit \"D;${__sai_ec}\"\n"
        "}\n"
        "PROMPT_COMMAND=__sai_prompt_command\n"
        "PS1='\\u@\\h:\\w\\$ \\[\\033]633;SAI;B\\007\\]'\n"
    )


def build_zsh_integration_script() -> str:
    # READY is literal in PROMPT with zsh zero-width %{ %}.
    return (
        "__sai_emit() { printf '\\033]633;SAI;%s\\007' \"$1\"; }\n"
        "__sai_precmd() {\n"
        "  local __sai_ec=$?\n"
        "  __sai_emit \"D;${__sai_ec}\"\n"
        "}\n"
        "autoload -Uz add-zsh-hook\n"
        "add-zsh-hook precmd __sai_precmd\n"
        "PROMPT='%n@%m:%~%# %{'$'\\033]633;SAI;B\\007''%}'\n"
    )


def integration_script_for_shell(shell: str) -> str:
    if shell == SHELL_POWERSHELL:
        return build_powershell_integration_script()
    if shell == SHELL_ZSH:
        return build_zsh_integration_script()
    if shell == SHELL_BASH:
        return build_bash_integration_script()
    raise ValueError(f"不支持为 shell={shell!r} 生成 Shell Integration 脚本。")
