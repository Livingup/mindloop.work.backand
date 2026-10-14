from __future__ import annotations

from threading import Lock

from agent_runtime.file_resource.factory import FileResourceServiceFactory
from agent_runtime.file_resource.service import FileResourceService

_file_resource_service: FileResourceService | None = None
_file_resource_service_lock = Lock()
_file_resource_upload_task_service = None
_file_resource_upload_task_service_lock = Lock()


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


def get_file_resource_upload_task_service():
    global _file_resource_upload_task_service
    if _file_resource_upload_task_service is not None:
        return _file_resource_upload_task_service
    with _file_resource_upload_task_service_lock:
        if _file_resource_upload_task_service is None:
            from agent_runtime.file_resource.upload_task_service import FileResourceUploadTaskService

            _file_resource_upload_task_service = FileResourceUploadTaskService(get_file_resource_service())
        return _file_resource_upload_task_service


def reset_file_resource_services_for_runtime_mode_change() -> None:
    """清理文件资源服务单例，以便 bootstrap 完成后按最新共享配置模式重建。"""
    global _file_resource_service, _file_resource_upload_task_service
    with _file_resource_upload_task_service_lock:
        _file_resource_upload_task_service = None
    with _file_resource_service_lock:
        _file_resource_service = None
