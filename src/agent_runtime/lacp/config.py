# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LacpConfig:
    enabled: bool = False
    registry_url: str = ""
    api_key: str | None = None
    data_dir: str = "~/.spread-ai/lacp"
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 5.0
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
