from __future__ import annotations

from threading import Lock

from agent_runtime.file_resource.factory import FileResourceServiceFactory
from agent_runtime.file_resource.service import FileResourceService

_file_resource_service: FileResourceService | None = None
_file_resource_service_lock = Lock()


def get_file_resource_service() -> FileResourceService:
    global _file_resource_service
    if _file_resource_service is not None:
        return _file_resource_service
    with _file_resource_service_lock:
        if _file_resource_service is None:
            from agent_runtime.config import ConfigRegistry

            _file_resource_service = FileResourceServiceFactory.create_from_config(
                ConfigRegistry.instance().file_resource_service,
            )
        return _file_resource_service
