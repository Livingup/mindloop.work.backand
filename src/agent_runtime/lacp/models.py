from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


LacpRegistrationStatus = Literal["disabled", "registering", "registered", "unregistering", "failed"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class LacpInstanceRegistration:
    instance_id: str
    role_name: str
    description: str
    workspace: str | None = None
    enabled: bool = False
    status: LacpRegistrationStatus = "disabled"
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    def with_state(
        self,
        *,
        enabled: bool | None = None,
        status: LacpRegistrationStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> "LacpInstanceRegistration":
        return LacpInstanceRegistration(
            instance_id=self.instance_id,
            role_name=self.role_name,
            description=self.description,
            workspace=self.workspace,
            enabled=self.enabled if enabled is None else enabled,
            status=self.status if status is None else status,
            error_code=error_code,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "role_name": self.role_name,
            "description": self.description,
            "workspace": self.workspace,
            "enabled": self.enabled,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LacpInstanceRegistration":
        enabled = bool(data.get("enabled", False))
        status = str(data.get("status") or ("registered" if enabled else "disabled"))
        if status not in {"disabled", "registering", "registered", "unregistering", "failed"}:
            status = "disabled"
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            role_name=str(data.get("role_name") or "Agent"),
            description=str(data.get("description") or "AgentRuntime agent instance"),
            workspace=(str(data["workspace"]) if data.get("workspace") is not None else None),
            enabled=enabled,
            status=status,  # type: ignore[arg-type]
            error_code=(str(data["error_code"]) if data.get("error_code") is not None else None),
            error_message=(str(data["error_message"]) if data.get("error_message") is not None else None),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )


@dataclass(frozen=True, slots=True)
class LacpInboundMessage:
    message_id: str
    source_tag: str
    from_instance_id: str
    from_role_name: str
    to_instance_id: str
    content: str
    from_workspace: str | None = None
    timestamp: str | None = None

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> "LacpInboundMessage":
        return cls(
            message_id=str(frame.get("message_id") or ""),
            source_tag=str(frame.get("source_tag") or ""),
            from_instance_id=str(frame.get("from_instance_id") or ""),
            from_role_name=str(frame.get("from_role_name") or ""),
            from_workspace=(str(frame["from_workspace"]) if frame.get("from_workspace") is not None else None),
            to_instance_id=str(frame.get("to_instance_id") or ""),
            content=str(frame.get("content") or ""),
            timestamp=(str(frame["timestamp"]) if frame.get("timestamp") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class LacpExecutionObservation:
    instance_id: str
    execution_id: str
    event_id: str
    sequence: int
    state: str
    event: dict[str, Any]
    caller_instance_id: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)

    def to_frame(self, request_id: str) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "type": "execution_observation",
            "request_id": request_id,
            "instance_id": self.instance_id,
            "execution_id": self.execution_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "state": self.state,
            "event": self.event,
        }
        if self.caller_instance_id is not None:
            frame["caller_instance_id"] = self.caller_instance_id
        return frame


@dataclass(frozen=True, slots=True)
class LacpClientStatus:
    enabled: bool
    running: bool
    connected: bool
    registry_url: str
    ws_endpoint: str | None = None
    last_error: str | None = None
    last_connected_at: str | None = None
    last_disconnected_at: str | None = None
    reconnect_attempt: int = 0
