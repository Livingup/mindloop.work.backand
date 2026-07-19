from __future__ import annotations

import os
import shutil
import sys
from typing import Final


SHELL_POWERSHELL: Final[str] = "powershell"
SHELL_ZSH: Final[str] = "zsh"
SHELL_BASH: Final[str] = "bash"

_ALIAS_TO_CANONICAL: Final[dict[str, str]] = {
    "powershell": SHELL_POWERSHELL,
    "powershell.exe": SHELL_POWERSHELL,
    "zsh": SHELL_ZSH,
    "bash": SHELL_BASH,
    "bash.exe": SHELL_BASH,
}


def platform_supported_shell() -> str:
    if sys.platform.startswith("win"):
        return SHELL_POWERSHELL
    if sys.platform == "darwin":
        return SHELL_ZSH
    if sys.platform.startswith("linux"):
        return SHELL_BASH
    raise RuntimeError(
        f"当前操作系统不受支持: platform={sys.platform!r}。"
        "仅支持 Windows 10/11 内置 Windows PowerShell 5.1、macOS(zsh)、Linux(bash)。"
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
            "Windows=powershell.exe 5.1，macOS=zsh，Linux=bash；无 fallback。"
        )
    return supported


def require_shell_executable(shell: str) -> str:
    canonical = resolve_supported_shell(shell)
    path: str | None = None
    if canonical == SHELL_POWERSHELL and sys.platform.startswith("win"):
        system_path = os.path.expandvars(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        for candidate in (system_path, shutil.which("powershell.exe"), shutil.which("powershell")):
            if candidate and os.path.isfile(candidate):
                path = candidate
                break
    else:
        path = shutil.which(canonical)
    if not path:
        raise RuntimeError(
            f"当前平台要求的 shell 不可用: {canonical!r}。"
            "Windows 10/11 需要系统内置 Windows PowerShell 5.1 (powershell.exe)。"
        )
    return path
