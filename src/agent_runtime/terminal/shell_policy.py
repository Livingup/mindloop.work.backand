from __future__ import annotations

import os
import shutil
import sys
from typing import Final


SHELL_PWSH: Final[str] = "pwsh"
SHELL_ZSH: Final[str] = "zsh"
SHELL_BASH: Final[str] = "bash"

_ALIAS_TO_CANONICAL: Final[dict[str, str]] = {
    "pwsh": SHELL_PWSH,
    "pwsh.exe": SHELL_PWSH,
    "zsh": SHELL_ZSH,
    "bash": SHELL_BASH,
    "bash.exe": SHELL_BASH,
}


def platform_supported_shell() -> str:
    if sys.platform.startswith("win"):
        return SHELL_PWSH
    if sys.platform == "darwin":
        return SHELL_ZSH
    if sys.platform.startswith("linux"):
        return SHELL_BASH
    raise RuntimeError(
        f"当前操作系统不受支持: platform={sys.platform!r}。"
        "仅支持 Windows(pwsh 7)、macOS(zsh)、Linux(bash)。"
    )


def resolve_supported_shell(shell: str | None) -> str:
    supported = platform_supported_shell()
    if shell is None:
        return supported
    raw = shell.strip()
    if not raw or raw.lower() == "default":
        return supported
    canonical = _ALIAS_TO_CANONICAL.get(raw.lower())
    if canonical is None or canonical != supported:
        raise ValueError(
            f"当前平台仅支持 shell={supported!r}。收到: {shell!r}。"
            "Windows=pwsh，macOS=zsh，Linux=bash；无 fallback。"
        )
    return supported


def require_shell_executable(shell: str) -> str:
    canonical = resolve_supported_shell(shell)
    path = shutil.which(canonical)
    if not path and canonical == SHELL_PWSH and sys.platform.startswith("win"):
        for candidate in (
            os.path.expandvars(r"%ProgramFiles%\PowerShell\7\pwsh.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"),
        ):
            if candidate and os.path.isfile(candidate):
                path = candidate
                break
    if not path:
        raise RuntimeError(
            f"当前平台要求的 shell 不可用: {canonical!r}。"
            f"Windows 需要 PowerShell 7 (pwsh)。"
        )
    return path
