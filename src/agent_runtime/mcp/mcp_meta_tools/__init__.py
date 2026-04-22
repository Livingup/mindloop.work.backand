# -*- coding: utf-8 -*-
"""MCP 元工具子模块。

将 MCP 客户端能力封装为 5 个本地工具（通过 @tool 装饰器注册），
供 Agent 在对话中自主调用 MCP 服务。

依赖 mcp_client 子模块的北向网关契约（McpClientGateway）。
"""
