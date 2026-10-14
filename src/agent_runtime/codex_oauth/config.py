# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexOAuthConfig:
    """louyi-codex 本机存储配置。客户端指纹、私有协议与 session namespace 不可配置。"""

    data_dir: str = "~/.spread-ai/louyi-codex"
