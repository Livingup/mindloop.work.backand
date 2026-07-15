from __future__ import annotations


class TerminalCapacityExceededError(RuntimeError):
    """Raised when live terminal quotas reject create/mount."""

    error_code = "terminal_capacity_exceeded"

    def __init__(
        self,
        message: str,
        *,
        scope: str,
        limit: int,
        current: int,
        agent_instance_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.limit = limit
        self.current = current
        self.agent_instance_id = agent_instance_id

    def to_metadata(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "scope": self.scope,
            "limit": self.limit,
            "current": self.current,
            "agent_instance_id": self.agent_instance_id,
        }
