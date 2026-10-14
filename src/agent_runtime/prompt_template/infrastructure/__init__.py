"""提示词模板服务基础设施层。

包含仓储接口的具体实现（如 YAML 文件存储）。
"""
from agent_runtime.prompt_template.infrastructure.yaml_snippet_repository import (
    YamlFileSnippetRepository,
)

__all__ = [
    "YamlFileSnippetRepository",
]
