"""统一文件资源本地存储服务。

该模块负责把统一文件资源内容按业务规则落地到本地文件系统，
具体包括：资源路径规划、哈希分片目录、幂等写入、原子写入保护、读取与删除。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_runtime.file_resource.models import MIME_TYPE_TO_EXTENSION


@dataclass(frozen=True, slots=True)
class LocalStoredResourceContent:
    """本地已存储资源内容结果。"""

    # 资源本地存储绝对路径。
    local_storage_path: str


class LocalResourceStorage:
    """统一文件资源本地存储服务。

    该服务负责把统一文件资源的二进制内容按业务规则落地到本地文件系统，
    为资源访问提供本地热缓存读取能力，同时支持离线场景下的资源读取。

    本地存储路径规则：{root}/{category}/{hash[0:2]}/{hash}.{ext}
    - category: 资源类别一级目录（image / pdf）
    - hash[0:2]: 内容哈希前两位做二级分片目录（最多 256 个子目录，防止单目录文件过多）
    - hash.ext: 完整内容哈希作为文件名 + 原始扩展名（便于运维识别）
    """

    def __init__(self, local_storage_root_path: str) -> None:
        # 本地资源存储根目录。所有统一文件资源的本地副本都会存放在该目录下的分类子目录中。
        self._local_storage_root_path = Path(local_storage_root_path)

    def build_local_storage_path(
        self,
        resource_category: str,
        resource_content_hash: str,
        resource_mime_type: str,
    ) -> str:
        """根据资源类别、内容哈希和 MIME 类型规划本地存储绝对路径。

        路径结构为 {root}/{category}/{hash前两位}/{hash}.{ext}，
        通过哈希前缀分片避免单目录文件数量过多影响文件系统性能。
        """

        extension = self._resolve_extension(resource_mime_type)
        shard = resource_content_hash[:2]
        local_path = (
            self._local_storage_root_path
            / resource_category
            / shard
            / f"{resource_content_hash}.{extension}"
        )
        return str(local_path)

    def store_resource_content(
        self,
        resource_category: str,
        resource_content_hash: str,
        resource_mime_type: str,
        resource_content: bytes,
    ) -> LocalStoredResourceContent:
        """把资源二进制内容写入本地文件系统。

        写入策略：
        - 幂等性：目标文件已存在时直接跳过（相同内容哈希 = 相同内容）。
        - 原子性：先写入临时文件，写入完成后通过 rename 替换到最终路径，
          防止写入过程中断导致产生损坏文件。
        - 自动建目录：父目录不存在时自动递归创建。
        """

        local_storage_path = self.build_local_storage_path(
            resource_category=resource_category,
            resource_content_hash=resource_content_hash,
            resource_mime_type=resource_mime_type,
        )
        target_path = Path(local_storage_path)

        if target_path.exists():
            return LocalStoredResourceContent(local_storage_path=local_storage_path)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
        tmp_path.write_bytes(resource_content)
        tmp_path.replace(target_path)
        return LocalStoredResourceContent(local_storage_path=local_storage_path)

    def read_resource_content(self, local_storage_path: str) -> bytes:
        """从本地文件系统读取资源二进制内容。

        调用方提供资源档案中记录的本地存储路径，服务直接读取并返回完整二进制内容。
        """

        return Path(local_storage_path).read_bytes()

    def delete_resource_content(self, local_storage_path: str) -> None:
        """删除本地文件系统中的资源内容。

        文件不存在时静默返回，不抛出异常，保证删除操作的幂等性。
        """

        Path(local_storage_path).unlink(missing_ok=True)

    def _resolve_extension(self, resource_mime_type: str) -> str:
        """根据资源 MIME 类型解析本地文件扩展名。

        例如 image/jpeg → jpg，application/pdf → pdf。
        不支持的 MIME 类型抛出 ValueError。
        """

        extension = MIME_TYPE_TO_EXTENSION.get(resource_mime_type)
        if extension is None:
            raise ValueError(f"不支持的资源 MIME 类型扩展名映射: {resource_mime_type}")
        return extension
