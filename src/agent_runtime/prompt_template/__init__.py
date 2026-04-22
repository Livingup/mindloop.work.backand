"""提示词模板服务模块。

负责提示词可复用片段的增删改查管理。
Agent 定义中的系统提示词通过引用片段唯一描述名进行模板拼接。
"""
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)

__all__ = [
    "PromptTemplateConfig",
    "PromptTemplateFactory",
    "PromptSnippetGateway",
]
