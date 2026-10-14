import asyncio
from types import SimpleNamespace

from agent_runtime.mcp.mcp_client.mcp_metadata_cache import (
    JsonMcpMetadataSnapshotRepository,
    McpCachedResource,
    McpCachedTool,
    McpMetadataRegistry,
    McpServiceMetadataSnapshot,
)
from agent_runtime.mcp.mcp_client.mcp_metadata_refresh_manager import McpMetadataRefreshManager
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_transport_config import StdioTransportConfig
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    McpConnectionStatus,
    McpResourceInfo,
    McpToolInfo,
    McpToolSummary,
)
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_gateway_impl import McpClientGatewayImpl


def _definition(service_id: str = "cache-rag") -> McpServiceDefinition:
    return McpServiceDefinition(
        service_id=service_id,
        name="Cache RAG",
        description="源码业务描述知识库",
        transport=StdioTransportConfig(command="cache-rag-server"),
        enabled=True,
        usage_guide="本地使用指南",
    )


def _snapshot(service_id: str = "cache-rag") -> McpServiceMetadataSnapshot:
    return McpServiceMetadataSnapshot.successful(
        service_id=service_id,
        supports_tools=True,
        supports_resources=True,
        server_name="cache-rag",
        server_version="1.0.0",
        instructions="服务端指南\n\n本地使用指南",
        tools=(
            McpCachedTool(
                name="codebase_search",
                description="检索代码库业务知识",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
        resources=(
            McpCachedResource(
                uri="cache://projects",
                name="projects",
                description="项目列表",
                mime_type="application/json",
            ),
        ),
        refreshed_at=123.0,
    )


class _FakeMetadataRefreshManager:
    async def load_persisted_metadata(self) -> None:
        return None

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def refresh_once(self) -> None:
        return None


class _CacheOnlyService:
    def __init__(self, definition: McpServiceDefinition) -> None:
        self.definition = definition
        self.live_metadata_calls: list[str] = []

    def list_services(self):
        return [self.definition]

    def get_service(self, service_id: str):
        if service_id != self.definition.service_id:
            raise KeyError(service_id)
        return self.definition

    def get_connection_status(self, service_id: str):
        self.get_service(service_id)
        return McpConnectionStatus(service_id=service_id, connected=True, status="connected")

    async def get_service_detail(self, service_id: str):
        self.live_metadata_calls.append("get_service_detail")
        raise AssertionError("元数据详情不应走实时 MCP 网络调用")

    async def list_tools(self, service_id: str):
        self.live_metadata_calls.append("list_tools")
        raise AssertionError("工具列表不应走实时 MCP 网络调用")

    async def get_tool_detail(self, service_id: str, tool_names: list[str]):
        self.live_metadata_calls.append("get_tool_detail")
        raise AssertionError("工具详情不应走实时 MCP 网络调用")

    async def list_resources(self, service_id: str):
        self.live_metadata_calls.append("list_resources")
        raise AssertionError("资源列表不应走实时 MCP 网络调用")


class _RefreshService:
    def __init__(self, *, hang_tools: bool = False) -> None:
        self.definition = _definition()
        self.hang_tools = hang_tools

    def list_services(self):
        return [self.definition]

    def get_service(self, service_id: str):
        if service_id != self.definition.service_id:
            raise KeyError(service_id)
        return self.definition

    async def get_service_detail(self, service_id: str):
        self.get_service(service_id)
        return {
            "supports_tools": True,
            "supports_resources": True,
            "server_name": "cache-rag",
            "server_version": "1.0.1",
            "instructions": "刷新后的指南",
        }

    async def list_tools(self, service_id: str):
        self.get_service(service_id)
        if self.hang_tools:
            await asyncio.sleep(3600)
        return [McpToolSummary(name="codebase_search", description="检索代码库业务知识")]

    async def get_tool_detail(self, service_id: str, tool_names: list[str]):
        self.get_service(service_id)
        return [
            McpToolInfo(
                name="codebase_search",
                description="检索代码库业务知识",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ]

    async def list_resources(self, service_id: str):
        self.get_service(service_id)
        return [
            McpResourceInfo(
                uri="cache://projects",
                name="projects",
                description="项目列表",
                mime_type="application/json",
            ),
        ]


def test_gateway_metadata_methods_read_memory_cache_without_live_network(tmp_path):
    async def scenario() -> None:
        registry = McpMetadataRegistry()
        registry.hydrate([_snapshot()])
        repository = JsonMcpMetadataSnapshotRepository(lambda: str(tmp_path))
        service = _CacheOnlyService(_definition())
        gateway = McpClientGatewayImpl(
            service,
            SimpleNamespace(),
            metadata_registry=registry,
            snapshot_repository=repository,
            metadata_refresh_manager=_FakeMetadataRefreshManager(),
        )

        detail = await gateway.get_service_detail("cache-rag")
        tools = await gateway.list_tools("cache-rag")
        tool_details = await gateway.get_tool_detail("cache-rag", ["codebase_search"])
        resources = await gateway.list_resources("cache-rag")

        assert detail.server_name == "cache-rag"
        assert detail.instructions == "服务端指南\n\n本地使用指南"
        assert [(tool.name, tool.description) for tool in tools] == [("codebase_search", "检索代码库业务知识")]
        assert tool_details[0].input_schema["properties"]["query"]["type"] == "string"
        assert resources[0].uri == "cache://projects"
        assert service.live_metadata_calls == []

    asyncio.run(scenario())


def test_refresh_manager_loads_persisted_cache_before_polling_and_updates_memory_first(tmp_path):
    async def scenario() -> None:
        repository = JsonMcpMetadataSnapshotRepository(lambda: str(tmp_path))
        repository.save(_snapshot())
        registry = McpMetadataRegistry()
        service = _RefreshService()
        manager = McpMetadataRefreshManager(
            service=service,
            registry=registry,
            snapshot_repository=repository,
            operation_timeout_seconds=0.05,
        )

        await manager.load_persisted_metadata()
        assert registry.get_snapshot("cache-rag").server_version == "1.0.0"

        await manager.refresh_service("cache-rag")
        refreshed = registry.get_snapshot("cache-rag")
        assert refreshed is not None
        assert refreshed.server_version == "1.0.1"
        assert refreshed.last_refresh_succeeded is True
        await manager.stop()

        persisted = repository.load_all()[0]
        assert persisted.server_version == "1.0.1"
        assert persisted.tools[0].input_schema["properties"]["query"]["type"] == "string"

    asyncio.run(scenario())


def test_refresh_timeout_keeps_stale_cache_and_records_failure(tmp_path):
    async def scenario() -> None:
        repository = JsonMcpMetadataSnapshotRepository(lambda: str(tmp_path))
        old_snapshot = _snapshot()
        repository.save(old_snapshot)
        registry = McpMetadataRegistry()
        registry.hydrate([old_snapshot])
        manager = McpMetadataRefreshManager(
            service=_RefreshService(hang_tools=True),
            registry=registry,
            snapshot_repository=repository,
            operation_timeout_seconds=0.01,
        )

        await manager.refresh_service("cache-rag")
        snapshot = registry.get_snapshot("cache-rag")
        assert snapshot is not None
        assert snapshot.server_version == "1.0.0"
        assert snapshot.tools[0].name == "codebase_search"
        assert snapshot.last_refresh_succeeded is False
        assert "list_tools" in (snapshot.last_refresh_error or "")
        await manager.stop()

    asyncio.run(scenario())
