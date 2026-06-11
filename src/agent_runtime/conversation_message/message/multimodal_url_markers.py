# -*- coding: utf-8 -*-
"""多模态资源 URL 文本标记。

统一会话内部使用 resource_id 引用文件资源；渲染到目标 LLM 协议时，真实多模态 block
由协议适配器生成。同时在有公开 URL 的多模态 block 前后插入简短 XML 文本标记，
让模型在文本上下文中也能感知文件 URL，便于后续把 URL 传递给其它 Agent。
"""
from __future__ import annotations

from html import escape


def multimodal_file_open_marker(kind: str, url: str | None) -> str | None:
    if not url:
        return None
    return f'<multimodal_file type="{escape(kind, quote=True)}" url="{escape(url, quote=True)}">'


def multimodal_file_close_marker() -> str:
    return "</multimodal_file>"
