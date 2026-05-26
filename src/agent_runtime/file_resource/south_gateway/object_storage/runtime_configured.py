from __future__ import annotations

from agent_runtime.file_resource.settings_repository import (
    ObjectStorageRuntimeSettings,
    ObjectStorageSettingsRepository,
)
from agent_runtime.file_resource.south_gateway.object_storage.aliyun_oss import (
    AliyunOssObjectStorageGateway,
)
from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
    UploadedObjectStorageResource,
)


class ObjectStorageConfigurationError(RuntimeError):
    pass


class RuntimeConfiguredObjectStorageGateway(ObjectStorageGateway):
    def __init__(self, settings_repository: ObjectStorageSettingsRepository) -> None:
        self._settings_repository = settings_repository

    def upload_resource_content(
        self,
        resource_id: str,
        resource_content: bytes,
        resource_mime_type: str,
    ) -> UploadedObjectStorageResource:
        gateway = self._create_gateway(require_credentials=True)
        return gateway.upload_resource_content(
            resource_id=resource_id,
            resource_content=resource_content,
            resource_mime_type=resource_mime_type,
        )

    def delete_resource_content(self, resource_id: str) -> None:
        gateway = self._create_gateway(require_credentials=False)
        gateway.delete_resource_content(resource_id)

    def _create_gateway(self, *, require_credentials: bool) -> ObjectStorageGateway:
        settings = self._settings_repository.get_settings()
        provider = settings.gateway_provider.strip()
        if provider == "aliyun_oss":
            self._assert_aliyun_settings(settings, require_credentials=require_credentials)
            return AliyunOssObjectStorageGateway(
                object_storage_endpoint=settings.object_storage_endpoint,
                object_storage_region=settings.object_storage_region,
                object_storage_bucket_name=settings.object_storage_bucket_name,
                object_storage_public_base_url=settings.object_storage_public_base_url,
                object_storage_object_key_prefix=settings.object_storage_object_key_prefix,
                object_storage_access_key_id=settings.object_storage_access_key_id or "",
                object_storage_access_key_secret=settings.object_storage_access_key_secret or "",
                connect_timeout_seconds=settings.connect_timeout_seconds,
            )
        raise ObjectStorageConfigurationError(f"不支持的对象存储服务商: {provider}")

    def _assert_aliyun_settings(
        self,
        settings: ObjectStorageRuntimeSettings,
        *,
        require_credentials: bool,
    ) -> None:
        missing: list[str] = []
        if not settings.object_storage_endpoint.strip():
            missing.append("Endpoint")
        if not settings.object_storage_region.strip():
            missing.append("Region")
        if not settings.object_storage_bucket_name.strip():
            missing.append("Bucket")
        if not settings.object_storage_public_base_url.strip():
            missing.append("公共访问 URL 前缀")
        if require_credentials and not (settings.object_storage_access_key_id or "").strip():
            missing.append("AccessKey ID")
        if require_credentials and not (settings.object_storage_access_key_secret or "").strip():
            missing.append("AccessKey Secret")
        if missing:
            raise ObjectStorageConfigurationError(
                "请先在设置页配置完整的阿里云 OSS 参数: " + "、".join(missing)
            )
