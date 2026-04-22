"""验证阿里云 OSS 连接是否正常的快速测试脚本。

运行方式：
    python -m pytest tests/test_oss_connection.py -v -s
"""
from pathlib import Path

from agent_runtime.file_resource.factory import FileResourceServiceFactory


# YAML 配置文件路径。
YAML_CONFIG_PATH = str(Path(__file__).resolve().parent.parent / "config" / "agent_runtime.yaml")


def test_oss_upload_and_delete():
    """测试：上传一张测试图片到 OSS，验证返回 URL，然后删除。"""

    service = FileResourceServiceFactory.create_from_yaml_file(YAML_CONFIG_PATH)

    # 构造一个最小的 PNG 文件（1x1 红色像素）。
    import base64
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
        "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
    )
    tiny_png_bytes = base64.b64decode(tiny_png_b64)

    # 写临时文件。
    tmp_file = Path(__file__).parent / "_test_upload.png"
    tmp_file.write_bytes(tiny_png_bytes)

    try:
        from agent_runtime.file_resource.north_gateway.resource_create_contracts import (
            LocalFileCreateRequest,
        )

        results = service.create_resources_from_local_files([
            LocalFileCreateRequest(
                local_file_path=str(tmp_file),
                resource_mime_type="image/png",
            ),
        ])

        assert len(results) == 1
        result = results[0]
        print(f"\n✅ 上传成功!")
        print(f"   resource_id: {result.resource_id}")
        print(f"   oss_url:     {result.object_storage_url}")
        print(f"   local_path:  {result.local_storage_path}")

        # 清理：删除刚上传的资源。
        service.delete_resources([result.resource_id])
        print(f"   ✅ 删除成功!")

    finally:
        tmp_file.unlink(missing_ok=True)


if __name__ == "__main__":
    test_oss_upload_and_delete()
