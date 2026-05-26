"""阿里云国际 OSS 对象存储南向网关实现。

该实现用于把统一文件资源服务的对象存储协作请求适配到阿里云国际 OSS，
负责资源内容上传、资源内容删除以及对象访问地址构建。
"""
from __future__ import annotations

from contextlib import suppress
from math import ceil

from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
    ObjectStorageCancelChecker,
    ObjectStorageProgressCallback,
    ObjectStorageUploadCancelledError,
    UploadedObjectStorageResource,
)

_MULTIPART_UPLOAD_THRESHOLD_BYTES = 16 * 1024 * 1024
_MULTIPART_UPLOAD_PART_SIZE_BYTES = 8 * 1024 * 1024
_MIN_OSS_SOCKET_TIMEOUT_SECONDS = 120


class AliyunOssObjectStorageGateway(ObjectStorageGateway):
    """阿里云国际 OSS 对象存储南向网关实现。

    该实现用于把统一文件资源服务的对象存储协作请求适配到阿里云国际 OSS，
    负责资源内容上传、资源内容删除以及对象访问地址构建。
    使用 oss2 SDK V4 签名算法进行身份认证。
    """

    def __init__(
        self,
        object_storage_endpoint: str,
        object_storage_region: str,
        object_storage_bucket_name: str,
        object_storage_public_base_url: str,
        object_storage_object_key_prefix: str,
        object_storage_access_key_id: str,
        object_storage_access_key_secret: str,
        connect_timeout_seconds: int = 60,
    ) -> None:
        # OSS 服务接入地址。用于建立统一文件资源服务到阿里云国际 OSS 的网络连接。
        self._object_storage_endpoint = object_storage_endpoint

        # OSS 所在区域。V4 签名算法必须指定区域标识，例如 cn-hangzhou。
        self._object_storage_region = object_storage_region

        # OSS 桶名称。统一文件资源服务所有对象都会被写入该业务桶。
        self._object_storage_bucket_name = object_storage_bucket_name

        # OSS 公网访问地址前缀。用于对外拼装资源的远端访问 URL。
        self._object_storage_public_base_url = object_storage_public_base_url.rstrip("/")

        # OSS 对象键前缀。用于在桶内把统一文件资源服务的对象隔离到固定业务目录下。
        self._object_storage_object_key_prefix = object_storage_object_key_prefix.strip("/")

        # OSS 连接超时时间，单位秒。用于控制南向网关建立连接时的等待上限。
        self._connect_timeout_seconds = max(connect_timeout_seconds, _MIN_OSS_SOCKET_TIMEOUT_SECONDS)

        # OSS Bucket 客户端实例。通过 oss2 SDK 初始化，用于执行实际的对象上传和删除操作。
        self._oss_bucket = self._create_oss_bucket(
            object_storage_access_key_id=object_storage_access_key_id,
            object_storage_access_key_secret=object_storage_access_key_secret,
        )

    def upload_resource_content(
        self,
        resource_id: str,
        resource_content: bytes,
        resource_mime_type: str,
        *,
        progress_callback: ObjectStorageProgressCallback | None = None,
        cancel_checker: ObjectStorageCancelChecker | None = None,
    ) -> UploadedObjectStorageResource:
        """上传统一文件资源内容到阿里云国际 OSS。

        把资源二进制内容写入由对象键前缀和资源标识组成的固定对象路径，
        同时为 OSS 对象写入 Content-Type，保证后续通过公网地址访问时具备正确媒体类型。
        返回包含资源标识和对象存储访问地址的上传结果。
        """

        object_key = self._build_object_key(resource_id)
        self._ensure_not_cancelled(cancel_checker)
        if len(resource_content) >= _MULTIPART_UPLOAD_THRESHOLD_BYTES:
            self._multipart_upload_object(
                object_key=object_key,
                resource_content=resource_content,
                resource_mime_type=resource_mime_type,
                progress_callback=progress_callback,
                cancel_checker=cancel_checker,
            )
        else:
            self._ensure_not_cancelled(cancel_checker)
            self._oss_bucket.put_object(
                object_key,
                resource_content,
                headers={"Content-Type": resource_mime_type},
            )
            self._ensure_not_cancelled(cancel_checker)
            if progress_callback is not None:
                progress_callback(len(resource_content), len(resource_content))
        return UploadedObjectStorageResource(
            resource_id=resource_id,
            object_storage_url=self._build_public_url(resource_id),
        )

    def delete_resource_content(self, resource_id: str) -> None:
        """删除阿里云国际 OSS 中的统一文件资源内容。

        删除语义基于统一文件资源自己的 resource_id，
        而不是把桶、目录和对象名等厂商概念暴露给统一文件资源服务总编排层。
        """

        object_key = self._build_object_key(resource_id)
        self._oss_bucket.delete_object(object_key)

    def _build_object_key(self, resource_id: str) -> str:
        """构建统一文件资源在 OSS 中的对象键。

        对象键由业务前缀和资源标识拼接而成，
        保证统一文件资源服务的对象在桶内集中存放于固定业务目录下。
        """

        if self._object_storage_object_key_prefix:
            return f"{self._object_storage_object_key_prefix}/{resource_id}"
        return resource_id

    def _build_public_url(self, resource_id: str) -> str:
        """构建 OSS 公网访问地址。

        public_base_url 已包含对象键前缀，只需拼接资源标识即可。
        """

        return f"{self._object_storage_public_base_url}/{resource_id}"

    def _multipart_upload_object(
        self,
        *,
        object_key: str,
        resource_content: bytes,
        resource_mime_type: str,
        progress_callback: ObjectStorageProgressCallback | None,
        cancel_checker: ObjectStorageCancelChecker | None,
    ) -> None:
        try:
            import oss2
        except ImportError as import_error:
            raise RuntimeError(
                "统一文件资源服务启用 aliyun_oss 对象存储网关时缺少 oss2 依赖，请先安装 aliyun-oss2。"
            ) from import_error

        upload_id = self._oss_bucket.init_multipart_upload(
            object_key,
            headers={"Content-Type": resource_mime_type},
        ).upload_id
        parts = []
        try:
            part_count = ceil(len(resource_content) / _MULTIPART_UPLOAD_PART_SIZE_BYTES)
            for part_index in range(part_count):
                self._ensure_not_cancelled(cancel_checker)
                part_number = part_index + 1
                start = part_index * _MULTIPART_UPLOAD_PART_SIZE_BYTES
                end = min(start + _MULTIPART_UPLOAD_PART_SIZE_BYTES, len(resource_content))
                result = self._oss_bucket.upload_part(
                    object_key,
                    upload_id,
                    part_number,
                    resource_content[start:end],
                )
                self._ensure_not_cancelled(cancel_checker)
                parts.append(oss2.models.PartInfo(part_number, result.etag))
                if progress_callback is not None:
                    progress_callback(end, len(resource_content))
            self._ensure_not_cancelled(cancel_checker)
            self._oss_bucket.complete_multipart_upload(
                object_key,
                upload_id,
                parts,
                headers={"Content-Type": resource_mime_type},
            )
            self._ensure_not_cancelled(cancel_checker)
        except Exception:
            with suppress(Exception):
                self._oss_bucket.abort_multipart_upload(object_key, upload_id)
            raise

    def _ensure_not_cancelled(self, cancel_checker: ObjectStorageCancelChecker | None) -> None:
        if cancel_checker is not None and cancel_checker():
            raise ObjectStorageUploadCancelledError("对象存储上传已取消。")

    def _create_oss_bucket(
        self,
        object_storage_access_key_id: str,
        object_storage_access_key_secret: str,
    ):
        """创建阿里云 OSS Bucket 客户端。

        使用 oss2 SDK 的 V1 签名算法进行身份认证，
        并根据配置的 endpoint、桶名称和超时时间初始化 Bucket 客户端。
        如果运行环境未安装 oss2 依赖，抛出 RuntimeError 并提示安装指引。
        """

        try:
            import oss2
        except ImportError as import_error:
            raise RuntimeError(
                "统一文件资源服务启用 aliyun_oss 对象存储网关时缺少 oss2 依赖，请先安装 aliyun-oss2。"
            ) from import_error

        oss_auth = oss2.Auth(object_storage_access_key_id, object_storage_access_key_secret)
        return oss2.Bucket(
            oss_auth,
            self._object_storage_endpoint,
            self._object_storage_bucket_name,
            connect_timeout=self._connect_timeout_seconds,
        )
