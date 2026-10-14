from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

from agent_runtime.lacp.models import LacpInstanceRegistration


class LacpRegistrationRegistry:
    def __init__(self, data_dir_accessor) -> None:
        self._data_dir_accessor = data_dir_accessor
        self._lock = RLock()
        self._items: dict[str, LacpInstanceRegistration] | None = None

    def list_registrations(self) -> list[LacpInstanceRegistration]:
        with self._lock:
            return list(self._ensure_loaded().values())

    def get_registration(self, instance_id: str) -> LacpInstanceRegistration | None:
        with self._lock:
            return self._ensure_loaded().get(instance_id)

    def is_registered(self, instance_id: str) -> bool:
        item = self.get_registration(instance_id)
        return bool(item and item.enabled and item.status == "registered")

    def upsert_enabled(self, registration: LacpInstanceRegistration) -> LacpInstanceRegistration:
        with self._lock:
            items = self._ensure_loaded()
            updated = LacpInstanceRegistration(
                instance_id=registration.instance_id,
                role_name=registration.role_name,
                description=registration.description,
                workspace=registration.workspace,
                enabled=True,
                status="registering",
            )
            items[updated.instance_id] = updated
            self._save(items)
            return updated

    def mark_registering(self, instance_id: str) -> None:
        self._update_state(instance_id, status="registering")

    def mark_registered(self, instance_id: str) -> None:
        self._update_state(instance_id, status="registered")

    def mark_failed(self, instance_id: str, *, error_code: str | None = None, error_message: str | None = None) -> None:
        self._update_state(instance_id, status="failed", error_code=error_code, error_message=error_message)

    def mark_unregistered(self, instance_id: str) -> None:
        self._update_state(instance_id, enabled=False, status="disabled")

    def mark_disconnected(self) -> None:
        with self._lock:
            items = self._ensure_loaded()
            changed = False
            for instance_id, item in list(items.items()):
                if item.enabled and item.status == "registered":
                    items[instance_id] = item.with_state(status="registering")
                    changed = True
            if changed:
                self._save(items)

    def desired_enabled(self) -> list[LacpInstanceRegistration]:
        with self._lock:
            return [item for item in self._ensure_loaded().values() if item.enabled]

    def _update_state(self, instance_id: str, **kwargs) -> None:
        with self._lock:
            items = self._ensure_loaded()
            item = items.get(instance_id)
            if item is None:
                return
            items[instance_id] = item.with_state(**kwargs)
            self._save(items)

    def _ensure_loaded(self) -> dict[str, LacpInstanceRegistration]:
        if self._items is not None:
            return self._items
        path = self._file_path()
        if not path.exists():
            self._items = {}
            return self._items
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        items: dict[str, LacpInstanceRegistration] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    item = LacpInstanceRegistration.from_dict({"instance_id": key, **value})
                    if item.instance_id:
                        items[item.instance_id] = item
        self._items = items
        return self._items

    def _save(self, items: dict[str, LacpInstanceRegistration]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {instance_id: item.to_dict() for instance_id, item in sorted(items.items())}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _file_path(self) -> Path:
        return Path(str(self._data_dir_accessor())).expanduser() / "registrations.json"
