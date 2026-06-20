from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConnectionChannelConfig:
    """Machine-local remote browser connection channel configuration."""

    data_dir: str = "~/.spread-ai/connection-channel"
