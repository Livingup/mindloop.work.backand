from __future__ import annotations

from pydantic import BaseModel, Field

from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolResult, ToolRuntimeContext, build_tool_error_result
from agent_runtime.web_tools.models import ReadUrlContextRequest, WebSearchRequest, WebToolResult
from agent_runtime.web_tools.shared_service import get_web_tools_service


WEB_SEARCH_TOOL_DESCRIPTION = """Search one or more live-web topics through the configured native LLM web-search provider policy.

The tool is stateless: every search target creates an independent underlying LLM conversation and never reuses context from previous web_search calls.
Use it when the user asks for current, external, or source-grounded information. The runtime handles provider/model/API key selection, failover, concurrent multi-query execution, and returns an HTML result document."""

READ_URL_CONTEXT_TOOL_DESCRIPTION = """Read and summarize the context of a specific HTTP(S) URL through the configured native LLM web or URL-context provider policy.

The tool is stateless: every call creates an independent underlying LLM conversation and never reuses context from previous read_url_context calls.
Use it when the user gives a URL and asks for page context, summary, facts, or source-grounded extraction."""


class WebSearchToolRequest(BaseModel):
    query: str = Field(description="Search query to run on the live web.")
    additional_queries: list[str] = Field(default_factory=list, max_length=9, description="Additional live-web search queries to run concurrently in the same tool call.")
    max_results: int = Field(default=8, ge=1, le=50, description="Desired number of source results or source-grounded references.")
    allowed_domains: list[str] = Field(default_factory=list, description="Optional domains to restrict search results to.")
    blocked_domains: list[str] = Field(default_factory=list, description="Optional domains to exclude from search results.")
    recency: str | None = Field(default=None, description="Optional provider-specific recency filter, such as noLimit or recent time range when supported.")


class ReadUrlContextToolRequest(BaseModel):
    url: str = Field(description="HTTP or HTTPS URL to read.")
    additional_urls: list[str] = Field(default_factory=list, max_length=9, description="Additional HTTP(S) URLs to read concurrently in the same tool call.")
    question: str | None = Field(default=None, description="Optional focused question about the URL content.")
    max_summary_tokens: int | None = Field(default=None, ge=128, le=12000, description="Optional target summary budget for the returned page context.")


@tool(
    name="web_search",
    description=WEB_SEARCH_TOOL_DESCRIPTION,
    group="web_tools",
    public=True,
)
async def web_search(request: WebSearchToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    try:
        result = await get_web_tools_service().web_search(
            WebSearchRequest(
                query=request.query,
                additional_queries=tuple(request.additional_queries),
                max_results=request.max_results,
                allowed_domains=tuple(request.allowed_domains),
                blocked_domains=tuple(request.blocked_domains),
                recency=request.recency,
            ),
            tool_context=ctx
        )
    except Exception as exc:
        return build_tool_error_result(
            error_code="web_search_failed",
            error_message=str(exc),
        )
    return _tool_success_result(result, ctx)


@tool(
    name="read_url_context",
    description=READ_URL_CONTEXT_TOOL_DESCRIPTION,
    group="web_tools",
    public=True,
)
async def read_url_context(request: ReadUrlContextToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    try:
        result = await get_web_tools_service().read_url_context(
            ReadUrlContextRequest(
                url=request.url,
                additional_urls=tuple(request.additional_urls),
                question=request.question,
                max_summary_tokens=request.max_summary_tokens,
            ),
            tool_context=ctx
        )
    except Exception as exc:
        return build_tool_error_result(
            error_code="read_url_context_failed",
            error_message=str(exc),
        )
    return _tool_success_result(result, ctx)


def _tool_success_result(result: WebToolResult, ctx: ToolRuntimeContext) -> BaseToolResult:
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=result.content,
        metadata={
            "ui_kind": "web_tools",
            "operation": result.operation,
            "html_content": result.content,
            "preview_output": result.data,
            "provider_id": result.provider_id,
            "binding_id": result.binding_id,
            "model_id": result.model_id,
            "model_alias": result.model_alias,
            "api_format": result.api_format,
            "candidate_index": result.candidate_index,
            "attempt_count": result.attempt_count,
            "api_key_index": result.api_key_index,
            "request_id": ctx.request_id,
            "task_id": ctx.task_id,
            "agent_instance_id": ctx.agent_instance_id,
        },
    )
