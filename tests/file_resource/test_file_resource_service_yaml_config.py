from __future__ import annotations

import base64
import sys
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

PROJECT_ROOT_PATH = Path(__file__).resolve().parents[2]
SOURCE_ROOT_PATH = PROJECT_ROOT_PATH / "src"
if str(SOURCE_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT_PATH))

from agent_runtime.config import ConfigRegistry
from agent_runtime.file_resource.factory import FileResourceServiceFactory
from agent_runtime.file_resource.models import ImageResourceMetadata


def test_should_load_agent_runtime_yaml_config(tmp_path: Path) -> None:
    """验证统一文件资源服务能够正确加载 YAML 运行配置。"""

    yaml_file_path = _write_memory_yaml_config(tmp_path)

    ConfigRegistry.load(str(yaml_file_path))
    cfg = ConfigRegistry.instance()

    assert cfg.agent_runtime_service.service_instance_name == "agent-runtime-test"
    assert cfg.agent_runtime_service.service_environment == "test"
    assert cfg.agent_runtime_service.service_host == "127.0.0.1"
    assert cfg.agent_runtime_service.service_port == 18000
    assert cfg.file_resource_service.file_resource_batch_limit == 5
    assert cfg.file_resource_service.object_storage_gateway_provider == "memory"
    assert cfg.file_resource_service.object_storage_gateway.gateway_provider == "memory"
    assert cfg.conversation_repository.data_dir is not None

    ConfigRegistry.reset()


def test_should_create_file_resource_service_from_yaml_file_path(
    tmp_path: Path,
) -> None:
    """验证统一文件资源服务能够基于 YAML 配置文件完成服务装配。"""

    yaml_file_path = _write_memory_yaml_config(tmp_path)
    service = FileResourceServiceFactory.create_from_yaml_file(str(yaml_file_path))

    assert service is not None

    ConfigRegistry.reset()


def test_should_hot_reload_config_on_file_change(tmp_path: Path) -> None:
    """验证 watch=True 时修改 YAML 文件后配置自动热更新。"""

    yaml_file_path = _write_memory_yaml_config(tmp_path)
    ConfigRegistry.load(str(yaml_file_path), watch=True)

    assert ConfigRegistry.instance().agent_runtime_service.service_port == 18000

    # 修改 YAML 文件中的端口号。
    content = yaml_file_path.read_text(encoding="utf-8")
    yaml_file_path.write_text(
        content.replace("service_port: 18000", "service_port: 19000"),
        encoding="utf-8",
    )

    # 等待 watchdog 检测到文件变更并触发 reload。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ConfigRegistry.instance().agent_runtime_service.service_port == 19000:
            break
        time.sleep(0.2)

    assert ConfigRegistry.instance().agent_runtime_service.service_port == 19000

    ConfigRegistry.reset()


def _write_memory_yaml_config(tmp_path: Path) -> Path:
    """写入测试用统一文件资源服务 YAML 配置。"""

    local_resource_storage_root_path = (
        tmp_path / "runtime-data" / "file-resource"
    ).as_posix()
    conversation_data_dir = (
        tmp_path / "runtime-data" / "conversations"
    ).as_posix()
    yaml_file_path = tmp_path / "agent_runtime.yaml"
    yaml_file_path.write_text(
        f"""
agent_runtime_service:
  service_instance_name: "agent-runtime-test"
  service_environment: "test"
  service_host: "127.0.0.1"
  service_port: 18000

file_resource_service:
  file_resource_batch_limit: 5
  local_resource_storage_root_path: "{local_resource_storage_root_path}"
  file_resource_public_base_url: "http://127.0.0.1:18000/file-resource"
  enabled_resource_mime_types:
    - "image/png"
    - "application/pdf"
  object_storage_gateway_provider: "memory"
  object_storage_gateway:
    gateway_provider: "memory"
    object_storage_endpoint: ""
    object_storage_region: ""
    object_storage_bucket_name: ""
    object_storage_public_base_url: "https://memory-object-storage.test/resources"
    object_storage_object_key_prefix: ""
    object_storage_access_key_id: ""
    object_storage_access_key_secret: ""
    connect_timeout_seconds: 5
    read_timeout_seconds: 5

conversation_repository:
  data_dir: "{conversation_data_dir}"

skill_service:
  skills_dir: "{(tmp_path / 'skills').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return yaml_file_path


def _build_test_png_resource_bytes() -> bytes:
    """构建测试用 PNG 图片内容。"""

    image_buffer = BytesIO()
    test_image = Image.new("RGB", (10, 6), color=(0, 128, 255))
    test_image.save(image_buffer, format="PNG")
    return image_buffer.getvalue()
