"""统一文件资源元数据解析器。

该模块负责根据资源 MIME 类型识别资源类别，并从资源内容中提取结构化元数据。
"""
from __future__ import annotations

import re
from io import BytesIO

from agent_runtime.file_resource.models import (
    SUPPORTED_IMAGE_MIME_TYPES,
    SUPPORTED_MIME_TYPES,
    FileResourceMetadata,
    ImageResourceMetadata,
    PdfResourceMetadata,
)


class FileResourceMetadataParser:
    """统一文件资源元数据解析器。

    该解析器在资源接入流程中承担三项核心职责：
    1. MIME 类型校验：确保接入的资源类型在服务允许范围内。
    2. 资源类别判断：根据 MIME 类型确定资源属于 image 还是 pdf 类别。
    3. 结构化元数据提取：从资源二进制内容中解析出图片宽高或 PDF 页数。
    """

    def assert_supported_mime_type(self, resource_mime_type: str) -> str:
        """校验资源 MIME 类型是否在允许范围内。

        返回通过校验后的 MIME 类型原值，便于调用链继续复用。
        """

        if resource_mime_type not in SUPPORTED_MIME_TYPES:
            raise ValueError(f"不支持的统一文件资源 MIME 类型: {resource_mime_type}")
        return resource_mime_type

    def determine_resource_category(self, resource_mime_type: str) -> str:
        """根据资源 MIME 类型判断资源类别。

        资源类别是统一文件资源服务的一级分类维度，
        当前支持 image（图片）和 pdf（文档）两类，
        用于本地存储目录分类和元数据解析分派。
        """

        validated = self.assert_supported_mime_type(resource_mime_type)
        if validated in SUPPORTED_IMAGE_MIME_TYPES:
            return "image"
        return "pdf"

    def parse_metadata(
        self,
        resource_content: bytes,
        resource_mime_type: str,
    ) -> FileResourceMetadata:
        """从资源二进制内容中解析结构化元数据。

        根据 MIME 类型自动分发到对应的解析流程：
        - image 类别：解析图片宽度和高度（依赖 Pillow）。
        - pdf 类别：解析 PDF 总页数（优先使用 pypdf，否则回退到轻量规则解析）。
        """

        category = self.determine_resource_category(resource_mime_type)
        if category == "image":
            return self._parse_image_metadata(resource_content)
        return self._parse_pdf_metadata(resource_content)

    def _parse_image_metadata(self, resource_content: bytes) -> ImageResourceMetadata:
        """从图片二进制内容中解析宽度和高度。

        使用 Pillow 库打开图片并读取尺寸信息。
        如果运行环境未安装 Pillow 依赖，抛出 RuntimeError 并提示安装指引。
        """

        try:
            from PIL import Image
        except ImportError as import_error:
            raise RuntimeError(
                "统一文件资源服务解析图片元数据时缺少 Pillow 依赖，请先安装 Pillow。"
            ) from import_error

        with Image.open(BytesIO(resource_content)) as resource_image:
            image_width, image_height = resource_image.size

        return ImageResourceMetadata(
            image_width=image_width,
            image_height=image_height,
        )

    def _parse_pdf_metadata(self, resource_content: bytes) -> PdfResourceMetadata:
        """从 PDF 二进制内容中解析总页数。

        优先使用 pypdf 库进行精确解析。
        如果运行环境未安装 pypdf，自动回退到轻量正则规则解析。
        """

        try:
            from pypdf import PdfReader
        except ImportError:
            return self._parse_pdf_metadata_fallback(resource_content)

        pdf_reader = PdfReader(BytesIO(resource_content))
        return PdfResourceMetadata(pdf_page_count=len(pdf_reader.pages))

    def _parse_pdf_metadata_fallback(
        self,
        resource_content: bytes,
    ) -> PdfResourceMetadata:
        """在缺少 pypdf 依赖时，使用轻量规则统计 PDF 页数。

        通过统计 PDF 文本中的 /Type /Page 标记数量来推断页数。
        后续正式环境优先使用 pypdf。
        """

        pdf_text = resource_content.decode("latin-1", errors="ignore")
        pdf_page_count = len(re.findall(r"/Type\s*/Page(?!s)\b", pdf_text))
        if pdf_page_count <= 0:
            raise RuntimeError(
                "统一文件资源服务解析 PDF 元数据时缺少 pypdf 依赖，且无法通过轻量规则识别 PDF 页数。"
            )
        return PdfResourceMetadata(pdf_page_count=pdf_page_count)
