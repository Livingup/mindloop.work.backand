"""提示词模板服务模块。

负责提示词可复用片段的增删改查管理。
Agent 定义中的系统提示词通过引用片段唯一描述名进行模板拼接。
"""
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)
from agent_runtime.prompt_template.prompt_variables import (
    BUILTIN_PROMPT_VARIABLES,
    PromptVariableContext,
    PromptVariableDefinition,
    build_prompt_variable_values,
    get_builtin_prompt_variables,
    list_builtin_prompt_variables,
    render_prompt_variables,
)

__all__ = [
    "PromptTemplateConfig",
    "PromptTemplateFactory",
    "PromptSnippetGateway",
    "PromptVariableDefinition",
    "PromptVariableContext",
    "BUILTIN_PROMPT_VARIABLES",
    "get_builtin_prompt_variables",
    "list_builtin_prompt_variables",
    "build_prompt_variable_values",
    "render_prompt_variables",
]

