# LACP 客户端接入指南

> Agent App 客户端接入 LACP 注册中心的协议约定与事件规范 v0.4

---

## 1. 概述

本文档是 LACP 注册中心的 **LACP 客户端接入协议指南**，只描述 Agent App 客户端作为 **WebSocket 接入方**时需要遵守的协议约定：连接方式、帧格式、事件流程、必须遵守的规则。与编程语言无关。

### 1.1 LACP 协议体系组件

LACP 是基于 MCP 扩展的多 Agent 沟通协作协议体系。完整体系包含以下组件：

| 组件 | 通信/协议 | 职责 |
|----|------|------|
| **LACP Registry** | HTTP + WebSocket + MCP | 中心注册服务，维护实例注册表、消息路由、执行观察缓存、服务元信息和鉴权策略；同时暴露 `/ws`、`/mcp`、`/health`、`/.well-known/lacp`。 |
| **LACP 客户端** | WebSocket | Agent App 侧的 WS 接入组件，负责实例注册/注销、接收 Registry 推送的消息、回复消息确认、上报执行观察、心跳保活。 |
| **MCP Server** | MCP | Registry 对外暴露的 MCP 服务端，供 Agent/运行时发现实例、发送消息、查询状态。 |
| **MCP Client** | MCP | Agent/运行时侧的 MCP 客户端，连接 Registry MCP Server 并调用 MCP Tool。 |

### 1.2 你的角色

在 LACP 架构中，Agent App 客户端是 Registry 的 **WebSocket 接入方**，只负责把本地 Agent 实例接入 LACP Registry。

| 职责 | 通信方式 | 说明 |
|----|---------|------|
| **LACP WS 客户端** | WebSocket 长连接 | 负责实例注册/注销、接收 Registry 推送的消息、回复消息确认、上报执行观察、心跳保活 |

MCP 是 LACP 协议体系的一部分，但 MCP Client 是 Agent/运行时侧组件，不是 LACP 客户端职责。Agent/运行时如何连接 MCP、如何使用工具、如何组织提示词，是 Agent/运行时自己的集成问题。

### 1.3 客户端职责边界

一个符合 LACP 的 Agent App 客户端 MUST 实现以下职责：

| 职责 | 必须实现的行为 |
|----|------|
| **服务配置解析** | 读取用户/平台配置的 Registry Base URL 或完整 WS 端点；请求 `/.well-known/lacp` 获取 `endpoints.ws` 端点路径和 `auth` |
| **鉴权接入** | 根据 Registry 的 `auth` 策略，为 WebSocket 握手携带 API Key |
| **WS 连接管理** | 与 Registry 的 `/ws` 建立唯一一条 WebSocket 长连接；响应 Ping/Pong；断线后按退避策略重连 |
| **实例生命周期管理** | 为本客户端内每个 Agent 实例生成并持久维护 `instance_id`；实例启动后发送 `register`；实例关闭时发送 `unregister`；重连后重新注册所有在线实例 |
| **消息接收与路由** | 接收 Registry 推送的 `message` 帧；按 `to_instance_id` 路由到本地目标 Agent 实例；将消息作为 user message 插入该实例对话；立即回复 `ack` |
| **执行观察上报** | 在实例执行长任务时主动发送 `execution_observation`，上报状态变化、工具调用轨迹和阶段性工作日志；处理 `execution_observation_ack` |

客户端 MUST NOT 承担以下职责：

| 非职责 | 说明 |
|----|------|
| **不实现 Registry** | 客户端不维护全局注册表，不负责发现其他客户端的连接状态 |
| **不处理 MCP 通道** | 客户端不负责配置 MCP Server、不管理 MCP Transport、不替 Agent/运行时建立 MCP 会话、不处理工具或提示词集成 |
| **不改写业务消息内容** | Registry 推送的 `content` 必须原样作为业务内容使用；客户端只在插入对话时添加 `source_tag` 前缀用于来源识别 |
| **不等待状态查询帧** | 客户端不会再收到旧的 `query_status` 拉取帧；状态可观测性只通过主动上报 `execution_observation` 实现 |
| **不自行判断是否需要 API Key** | 是否需要 API Key 只由 Registry 的 `auth` 策略决定 |

### 1.4 最小实现路径

如果从零实现一个 LACP 客户端，推荐按以下顺序完成：

1. 支持配置 Registry Base URL 和 API Key。
2. 请求 `/.well-known/lacp`，解析 `endpoints.ws` 端点路径和 `auth`。
3. 基于已配置的 Registry Base URL 和 `endpoints.ws` 解析完整 WebSocket URL，并按 `auth` 策略携带 API Key 建连。
4. 为每个本地 Agent 实例生成稳定的 `instance_id`，发送 `register`。
5. 接收 `message` 帧，按 `to_instance_id` 投递到目标实例对话，并回复 `ack`。
6. 在 Agent 执行任务过程中主动上报 `execution_observation`。
7. 实现 Ping/Pong、断线重连、重连后重新注册、实例关闭时注销。

### 1.5 通信通道

```
你的客户端                                 注册中心
┌────────────────────┐                ┌──────────────────┐
│                    │    唯一 WS     │                  │
│  客户端程序本身     │◄══════════════►│  /ws 端点        │
│  (注册/注销/收消息)  │    长连接       │                  │
│                    │                │                  │
└────────────────────┘                └──────────────────┘
```

MCP 通道不在上图中。它是 Agent/运行时与 Registry `/mcp` 之间的独立协议通道，不是 LACP 客户端职责。

**关键规则：**
- 一个客户端与注册中心之间只建立**一条** WebSocket 连接
- 该客户端管理的所有 Agent 实例**共享**这一条连接
- 注册中心通过帧中的 `instance_id` 字段区分不同实例

## 2. 接入前提

### 2.1 Registry 服务地址配置

客户端接入 LACP 前，MUST 获得 Registry 服务地址。Registry 是一个可本地部署或远程部署的服务；客户端不应假设 Registry 一定位于本机，也不应依赖本地文件自动发现。

Registry 服务端的配置文件、启动命令、本地/远程部署方式见 `LACP-registry-deployment.md`。本文档只描述 LACP 客户端如何接入已经部署好的 Registry 服务。

| 项目 | 说明 |
|----|------|
| Registry Base URL | 客户端 SHOULD 允许用户、平台或工作空间显式配置 Registry 服务基础地址，例如 `http://127.0.0.1:12589` 或 `https://registry.example.com`。 |
| WebSocket 端点 | 客户端可以由 Base URL 按 `/ws` 拼接，也可以从服务元信息接口读取 `endpoints.ws` 端点路径后基于 Base URL 解析。 |
| 服务元信息端点 | Registry SHOULD 暴露 `/.well-known/lacp`，返回端点路径和鉴权策略。LACP 客户端只需要使用其中的 `endpoints.ws` 和 `auth`。 |
| API Key | 是否需要 API Key 由 Registry 服务配置中的 `auth.required` 决定，并通过服务元信息中的 `auth.type` 暴露给客户端。**不能仅根据本地/远程判断是否需要 API Key**。 |

### 2.2 鉴权规则

- 如果服务元信息声明 `auth.type = "api_key"`，客户端 MUST 为 WebSocket 连接携带 API Key。
- 如果 Registry 服务端启用了鉴权，即使服务端地址是 `127.0.0.1`，客户端也 MUST 携带 API Key。
- 如果 Registry 服务端未启用鉴权，协议层不会要求 API Key；但远程生产部署 SHOULD 启用 API Key 与 HTTPS/WSS。
- 推荐请求头格式：`Authorization: Bearer <api-key>`。如果客户端 WebSocket 库无法设置请求头，MAY 使用服务端明确支持的替代方式，例如 `api_key` 查询参数。

### 2.3 通信格式约定

- 所有 WS 通信使用 **JSON 文本帧**
- 每个帧 MUST 包含 `type` 字段，用于标识帧类型
- 请求-响应配对通过 `request_id` 字段关联：
  - 客户端发送请求帧时携带自己生成的 `request_id`
  - 注册中心在响应帧中原样返回该 `request_id`
  - 客户端据此匹配响应
- 所有时间戳使用 **ISO 8601** 格式（如 `2025-01-15T10:30:00Z`）

## 3. 配置 Registry 服务端

客户端启动时，MUST 先解析 Registry 服务配置，得到 `ws_endpoint` 和可选 `auth` 信息。推荐流程是：用户或平台配置 Registry Base URL，客户端请求该 Base URL 下的 `/.well-known/lacp` 获取端点路径和鉴权策略；如果服务元信息不可用，客户端 MAY 按约定从 Base URL 拼接 `/ws`。

### 3.1 显式配置格式

客户端产品 SHOULD 支持等价于以下结构的显式配置：

```json
{
  "base_url": "https://registry.example.com",
  "api_key": "<由用户或平台安全注入，不应硬编码>"
}
```

客户端内部解析后 SHOULD 归一化为完整连接配置：

```json
{
  "endpoints": {
    "ws": "wss://registry.example.com/ws",
    "health": "https://registry.example.com/health",
    "metadata": "https://registry.example.com/.well-known/lacp"
  },
  "auth": {
    "type": "api_key",
    "header": "authorization",
    "scheme": "Bearer"
  }
}
```

### 3.2 服务元信息接口

Registry SHOULD 在 `/.well-known/lacp` 返回服务元信息：

```json
{
  "name": "LACP Registry",
  "status": "running",
  "endpoints": {
    "ws": "/ws",
    "health": "/health",
    "metadata": "/.well-known/lacp"
  },
  "auth": {
    "type": "api_key",
    "header": "authorization",
    "scheme": "Bearer"
  }
}
```

LACP 客户端只需要读取 `endpoints.ws` 和 `auth`。`endpoints.ws` 是相对于已配置 Registry Base URL 的端点路径；客户端 MUST 使用自己已配置的 Base URL 将其解析为完整 WebSocket URL。服务元信息中可能包含面向其他调用方的端点，但这些端点不属于 LACP 客户端实现职责。

### 3.3 配置解析规则

- 客户端 MUST 支持配置 Registry Base URL 或完整 WS 端点。
- 客户端 SHOULD 优先使用服务元信息接口返回的 `endpoints.ws` 端点路径和 `auth` 策略。
- 如果没有任何 Registry 服务配置，客户端 SHOULD 提示用户配置 Registry 服务地址。
- 当服务元信息返回 `auth.type = "api_key"` 但客户端没有可用 API Key 时，客户端 MUST 拒绝连接并提示用户配置 API Key。

## 4. WebSocket 连接

### 4.1 连接端点

```
<configured ws_endpoint, e.g. ws://127.0.0.1:12589/ws or wss://registry.example.com/ws>
```

- 客户端 MUST 使用解析后的完整 `ws_endpoint` 建连，不应把本地默认地址写死。
- 如果鉴权策略要求 API Key，客户端 MUST 在 WS 握手请求中携带 API Key，连接建立后再发送注册消息。

### 4.2 连接规则

| 规则 | 说明 |
|----|------|
| 连接数量 | 一个 Agent App 客户端与注册中心之间 MUST 只建立**一条**连接 |
| 实例共享 | 该客户端管理的所有 Agent 实例共享同一条连接 |
| 实例区分 | 注册中心通过消息中的 `instance_id` 字段区分同一连接上的不同实例 |
| 无需 HTTP 服务 | 客户端无需自行启动 HTTP 服务，所有交互均通过此 WS 连接完成 |

### 4.3 连接断开处理

当 WS 连接断开时（无论主动关闭还是异常断开）：
- 注册中心会将该连接上**所有已注册的实例**从注册表中移除
- 这些实例将不再可被其他 Agent 发现或接收消息
- Registry 不保存离线消息；断链期间发往这些实例的 `lacp_send_message` MUST 立即返回失败
- Registry 不保证断链后的执行观察缓存恢复；连接清理后，对这些实例的 `lacp_query_status` MUST 返回不存在
- 客户端 SHOULD 启动重连流程（见第10章）

## 5. 实例注册

WS 连接建立后，客户端通过发送 `register` 帧将 Agent 实例注册到注册中心。注册成功后，该实例方可被其他 Agent 发现和通信。

### 5.1 注册请求帧（客户端 → 注册中心）

```json
{
  "type": "register",
  "request_id": "req-001",
  "instance_id": "claude-code-001",
  "role_name": "Claude Code",
  "description": "专注于代码编写、调试与工程化的AI编程助手",
  "workspace": "C:/project/my-app"
}
```

| 字段 | 类型 | 必选 | 说明 |
|----|------|:----:|------|
| `type` | string | 是 | 固定 `"register"` |
| `request_id` | string | 是 | 客户端生成的请求唯一标识，用于匹配响应 |
| `instance_id` | string | 是 | Agent 实例唯一标识，由客户端自行生成并维护，格式不限 |
| `role_name` | string | 是 | 角色名称，人类可读标识。如 `"Claude Code"`、`"Manus"` |
| `description` | string | 是 | 实例功能介绍，供其他 Agent 发现时了解该实例的能力 |
| `workspace` | string | 否 | 工作空间路径，用于区分同一角色的不同实例 |

### 5.2 注册响应帧（注册中心 → 客户端）

**成功：**
```json
{
  "type": "register_ack",
  "request_id": "req-001",
  "success": true
}
```

**失败：**
```json
{
  "type": "register_ack",
  "request_id": "req-001",
  "success": false,
  "error_code": "REGISTER.INVALID_PARAMS",
  "error_message": "Missing required fields: instance_id, role_name, description"
}
```

| 字段 | 类型 | 说明 |
|----|------|------|
| `type` | string | 固定 `"register_ack"` |
| `request_id` | string | 对应请求的 `request_id` |
| `success` | boolean | 是否成功 |
| `error_code` | string | 失败时的错误码（见第13章） |
| `error_message` | string | 失败时的错误描述 |

### 5.3 注册规则

| 规则 | 说明 |
|----|------|
| 重复注册 | 同一 `instance_id` 再次注册视为**更新信息**（如更新 description），不报错 |
| ID 格式 | `instance_id` 的格式和生成规则由客户端自行决定，仅要求全局唯一 |
| 注册时机 | WS 连接建立后、Agent 实例创建后，立即发送注册 |
| 在线状态 | 注册成功后实例立即标记为 `online`，可被其他 Agent 发现 |

## 6. 实例注销

Agent 实例关闭时，客户端 SHOULD 通过 WS 发送注销消息。所有实例注销完毕且不再需要通信时，可断开 WS 连接。

### 6.1 注销请求帧（客户端 → 注册中心）

```json
{
  "type": "unregister",
  "request_id": "req-002",
  "instance_id": "claude-code-001"
}
```

| 字段 | 类型 | 必选 | 说明 |
|----|------|:----:|------|
| `type` | string | 是 | 固定 `"unregister"` |
| `request_id` | string | 是 | 客户端生成的请求唯一标识 |
| `instance_id` | string | 是 | 要注销的 Agent 实例 ID |

### 6.2 注销响应帧（注册中心 → 客户端）

```json
{
  "type": "unregister_ack",
  "request_id": "req-002",
  "success": true
}
```

| 字段 | 类型 | 说明 |
|----|------|------|
| `type` | string | 固定 `"unregister_ack"` |
| `request_id` | string | 对应请求的 `request_id` |
| `success` | boolean | 是否成功 |
| `error_code` | string | 失败时的错误码 |
| `error_message` | string | 失败时的错误描述 |

### 6.3 注销规则

| 规则 | 说明 |
|----|------|
| 幂等性 | 对不存在的 `instance_id` 注销，返回成功（不报错） |
| 连接不受影响 | 注销实例不影响 WS 连接本身，同一连接上的其他实例可继续使用 |
| 效果 | 注销成功后，实例从注册表移除，不再可被发现或接收消息 |

## 7. 接收消息推送

这是 LACP 最核心的事件。当其他 Agent 向你的实例发送消息时，注册中心会通过 WS 推送消息帧到你的客户端。

### 7.1 推送消息帧（注册中心 → 客户端）

```json
{
  "type": "message",
  "message_id": "msg-20250115-a1b2c3",
  "source_tag": "@Manus manus-a3f8",
  "from_instance_id": "manus-a3f8",
  "from_role_name": "Manus",
  "from_workspace": "C:/project/LACP",
  "to_instance_id": "claude-code-001",
  "content": "帮我测试下这个页面",
  "attachments": [
    {"kind": "image", "url": "https://example.test/screenshot.png", "name": "screenshot.png", "mime_type": "image/png"},
    {"kind": "pdf", "url": "https://example.test/spec.pdf", "name": "spec.pdf", "mime_type": "application/pdf"}
  ],
  "timestamp": "2025-01-15T10:30:00Z"
}
```

| 字段 | 类型 | 说明 |
|----|------|------|
| `type` | string | 固定 `"message"` |
| `message_id` | string | 消息唯一标识，由注册中心生成，用于确认回复 |
| `source_tag` | string | 来源标记，格式 `@角色名 实例ID` |
| `from_instance_id` | string | 发送方实例 ID |
| `from_role_name` | string | 发送方角色名称 |
| `from_workspace` | string | 发送方工作目录，可选；用于客户端标题展示。若发送方未注册 workspace，则该字段缺省，且 MUST 不拼接进 `content` |
| `to_instance_id` | string | 目标实例 ID。客户端 MUST 使用该字段把消息路由到正确的本地 Agent 实例对话 |
| `content` | string | 消息内容，原样透传；attachments-only 消息中该字段为空字符串 |
| `attachments` | array | 可选结构化附件列表。每项包含 `kind`（`image` 或 `pdf`）、`url`（http/https 远程文件 URL）、可选 `name`、可选 `mime_type`；客户端 MUST 按 URL-first 方式接入资源，不得从 `content` 中解析附件 URL |
| `timestamp` | string | 发送时间，ISO 8601 |

### 7.2 确认帧（客户端 → 注册中心）

收到消息后，客户端 MUST 回复确认帧：

```json
{
  "type": "ack",
  "message_id": "msg-20250115-a1b2c3",
  "received": true
}
```

| 字段 | 类型 | 说明 |
|----|------|------|
| `type` | string | 固定 `"ack"` |
| `message_id` | string | 对应推送消息的 `message_id` |
| `received` | boolean | `true` 表示消息已接收，且文本/附件已完成客户端侧接入与校验，并已作为 user message 插入到该推送帧 `to_instance_id` 指定的目标实例对话；带附件消息若远程 URL 接入、文件类型校验或统一文件输入限制校验失败，应返回 `false` |

### 7.3 消息处理规则

| 规则 | 说明 |
|----|------|
| **MUST 回复 ack** | 收到 `message` 帧后 MUST 回复 `ack` 帧，否则注册中心在 **5 秒**后视为投递失败 |
| **插入 user message** | 客户端 MUST 将消息作为一条 **user message** 插入到 `to_instance_id` 指定的目标 Agent 实例当前对话中 |
| **插入格式** | 文本显示仍为 `【{source_tag}】{content}`，如：`【@Manus manus-a3f8】帮我测试下这个页面`；附件作为结构化资源随消息入队，不拼接进文本 |
| **附件接入** | 客户端收到 `attachments[]` 后，MUST 按 image/pdf 远程 URL 创建本地文件资源，并在入队前复用自身统一文件输入限制策略；超限或类型不匹配时不得入队成功 |
| **实例路由** | 客户端 MUST 根据 `to_instance_id` 路由消息到正确的本地 Agent 实例，不能依赖共享 WebSocket 连接上下文推断目标实例 |

## 8. 上报执行观察

客户端不再等待注册中心发送 `query_status` 帧。执行状态由客户端主动通过 WS 上报，注册中心缓存最近执行快照；其他调用方查询状态时会读取该缓存。

### 8.1 执行观察帧（客户端 → 注册中心）

```json
{
  "type": "execution_observation",
  "request_id": "obs-req-001",
  "instance_id": "claude-code-001",
  "execution_id": "exec-20250115-001",
  "caller_instance_id": "manus-a3f8",
  "event_id": "obs-evt-001",
  "sequence": 1,
  "timestamp": "2025-01-15T10:30:00Z",
  "state": "executing",
  "event": {
    "kind": "tool_call",
    "summary": "正在执行代码 review",
    "tool": {
      "call_id": "tool-001",
      "name": "grep_search",
      "status": "started",
      "purpose": "定位相关实现"
    }
  }
}
```

| 字段 | 类型 | 必选 | 说明 |
|----|------|:----:|------|
| `type` | string | 是 | 固定 `"execution_observation"` |
| `request_id` | string | 是 | 客户端生成的请求唯一标识 |
| `instance_id` | string | 是 | 上报观察的实例 ID，必须已在同一 WS 连接注册 |
| `execution_id` | string | 是 | 一次任务/执行过程的唯一标识 |
| `caller_instance_id` | string | 否 | 调用方实例 ID |
| `event_id` | string | 是 | 本次观察事件唯一标识 |
| `sequence` | number | 是 | 同一执行内单调递增的事件序号 |
| `timestamp` | string | 是 | 事件发生时间，ISO 8601 |
| `state` | string | 是 | `executing`、`waiting_for_message`、`completed_waiting_caller` |
| `event` | object | 是 | 观察事件对象 |

### 8.2 观察事件对象

| 字段 | 类型 | 必选 | 说明 |
|----|------|:----:|------|
| `kind` | string | 是 | `state_changed`、`tool_call`、`work_log` |
| `summary` | string | 是 | 简短状态或事件摘要 |
| `detail` | string | 否 | 更详细的过程说明 |
| `related_message_id` | string | 否 | 关联的 LACP 消息 ID |
| `tool` | object | 否 | 工具调用观察信息，包含 `call_id`、`name`、`status`、`purpose`、`input_summary`、`output_summary`、`error_message`、`started_at`、`finished_at`、`duration_ms` |
| `waiting_for` | object | 否 | 等待对象和原因，用于 `waiting_for_message` |
| `caller_expected_action` | string | 否 | `new_instruction`、`end_task`、`confirm_or_continue` |
| `metadata` | object | 否 | 客户端扩展元数据 |

### 8.3 观察确认帧（注册中心 → 客户端）

注册中心收到观察帧后返回：

```json
{
  "type": "execution_observation_ack",
  "request_id": "obs-req-001",
  "instance_id": "claude-code-001",
  "execution_id": "exec-20250115-001",
  "event_id": "obs-evt-001",
  "sequence": 1,
  "success": true
}
```

### 8.4 上报规则

| 规则 | 说明 |
|----|------|
| 主动上报 | 客户端 SHOULD 在状态变化、工具调用开始/完成/失败、阶段性工作日志时发送 `execution_observation` |
| 权限边界 | 只能为当前 WS 连接已注册的 `instance_id` 上报观察 |
| 序号递增 | 同一 `execution_id` 的 `sequence` SHOULD 单调递增 |
| 长任务观测 | 执行观察只用于监控长任务状态，不作为 Agent 间业务消息；业务消息由 Registry 通过 `message` 帧投递并插入对话 |
## 9. 心跳与保活

注册中心通过 WebSocket 协议标准的 **Ping/Pong 帧**检测连接存活。

### 9.1 心跳机制

| 参数 | 值 |
|----|------|
| 发起方 | 注册中心（服务端 → 客户端） |
| 帧类型 | WebSocket 协议标准 Ping 帧（非 JSON 文本帧） |
| 发送间隔 | **30 秒**（可配置） |
| 容忍次数 | 连续 **3 次**未收到 Pong 响应则判定离线 |

### 9.2 客户端义务

- 客户端 MUST 响应来自注册中心的 Ping 帧（发送 Pong）
- 大多数 WebSocket 客户端库会**自动处理** Pong 响应，无需手动实现
- 如果你的 WS 库不自动处理，需手动监听 Ping 事件并回复 Pong

### 9.3 超时断连

连续 3 次未收到 Pong 响应时：
1. 注册中心关闭该 WebSocket 连接
2. 该连接上所有已注册实例从注册表中**移除**
3. 这些实例不再可被发现或接收消息

## 10. 断线重连

WS 连接意外断开时，客户端 SHOULD 自动重连并重新注册所有实例。

### 10.1 重连策略：指数退避

| 参数 | 值 |
|----|------|
| 初始等待 | **1 秒** |
| 退避倍数 | **2 倍** |
| 最大间隔 | **30 秒**（封顶） |
| 重连次数 | **无限制**（持续重试直到连上或客户端退出） |

**延迟计算公式：**

```
delay = min(1秒 × 2^重试次数, 30秒)

第1次: 1秒
第2次: 2秒
第3次: 4秒
第4次: 8秒
第5次: 16秒
第6次+: 30秒（封顶）
```

### 10.2 重连后必须重新注册

重连成功后，客户端 MUST 重新发送所有之前已注册实例的 `register` 消息（携带**相同的** `instance_id`）。

- 注册中心收到相同 `instance_id` 的注册消息时，视为更新注册信息
- 若断连期间注册中心已将实例移除（心跳超时），重新注册等效于新注册
- 效果相同：实例恢复为在线状态

## 11. 错误码参考

所有错误码采用 `分类.具体错误` 格式。

### 11.1 注册相关

| 错误码 | 含义 |
|----|------|
| `REGISTER.INVALID_PARAMS` | 注册参数不完整或格式错误（缺少 `instance_id`、`role_name` 或 `description`） |

### 11.2 注销相关

| 错误码 | 含义 |
|----|------|
| `UNREGISTER.INVALID_PARAMS` | 注销参数不完整或格式错误（缺少 `instance_id`） |

### 11.3 消息投递相关

| 错误码 | 含义 |
|----|------|
| `SEND.TARGET_NOT_FOUND` | 目标 `to_instance_id` 不存在于注册表中 |
| `SEND.SENDER_NOT_REGISTERED` | 发送方 `from_instance_id` 未注册，拒绝发送 |
| `SEND.DELIVERY_FAILED` | 消息推送后未收到确认帧（5 秒超时），投递失败 |
| `SEND.INVALID_PARAMS` | 发送参数不完整（缺少必填字段） |

### 11.4 查询相关

| 错误码 | 含义 |
|----|------|
| `QUERY.NOT_FOUND` | 查询的 `instance_id` 不存在于注册表中 |

### 11.5 通用

| 错误码 | 含义 |
|----|------|
| `INTERNAL_ERROR` | 注册中心内部错误 |

## 12. WS 帧速查表

### 12.1 客户端 → 注册中心

| `type` | 触发场景 | 关键字段 | 期望响应 |
|----|---------|---------|---------|
| `register` | 实例创建后注册 | `request_id`, `instance_id`, `role_name`, `description`, `workspace`(可选) | `register_ack` |
| `unregister` | 实例关闭时注销 | `request_id`, `instance_id` | `unregister_ack` |
| `ack` | 收到消息推送后确认 | `message_id`, `received` | 无 |
| `execution_observation` | 执行状态变化、工具调用、工作日志上报 | `request_id`, `instance_id`, `execution_id`, `event_id`, `sequence`, `state`, `event` | `execution_observation_ack` |

### 12.2 注册中心 → 客户端

| `type` | 触发场景 | 关键字段 | 客户端义务 |
|----|---------|---------|---------|
| `register_ack` | 注册结果 | `request_id`, `success`, `error_code`(失败时) | 处理结果 |
| `unregister_ack` | 注销结果 | `request_id`, `success`, `error_code`(失败时) | 处理结果 |
| `message` | 有消息送达 | `message_id`, `source_tag`, `from_instance_id`, `from_role_name`, `to_instance_id`, `content`, 可选 `attachments`, `timestamp` | MUST 回复 `ack`，MUST 按 `to_instance_id` 路由并插入 user message；有附件时 MUST 先完成 URL 资源接入与文件输入限制校验 |
| `execution_observation_ack` | 执行观察上报结果 | `request_id`, `success`, `error_code`(失败时) | 处理结果 |
| Ping 帧 | 心跳检测 | (WebSocket 协议帧，非 JSON) | MUST 回复 Pong（通常自动处理） |

## 13. 完整生命周期时序

以下展示一个 Agent App 客户端从启动到通信、执行观察上报、退出的完整事件流：

```
客户端                                    注册中心                              其他Agent
  │                                        │                                    │
  │ ① 解析 Registry 连接配置                  │                                    │
  │                                        │                                    │
  │ ② WS 连接（鉴权策略要求时携带 API Key）    │                                    │
  │ ═══════════════════════════════════════>│                                    │
  │                                        │                                    │
  │ ③ 发送 register                         │                                    │
  │ ──────────────────────────────────────>│                                    │
  │ <────────────────────────────────────── │                                    │
  │   register_ack {success: true}          │                                    │
  │                                        │                                    │
  │                                        │  (其他调用方触发消息投递)              │
  │                                        │ <────────────请求───────────────────│
  │                                        │                                    │
  │ ④ 收到 message 推送                      │                                    │
  │ <══════════════════════════════════════ │                                    │
  │                                        │                                    │
  │ ⑤ 回复 ack                               │                                    │
  │ ══════════════════════════════════════>│                                    │
  │                                        │ ───────返回投递结果────────────────>│
  │ ⑥ 插入 user message 到 Agent 对话         │                                    │
  │   【@角色名 实例ID】消息内容               │                                    │
  │                                        │                                    │
  │ ⑦ 执行过程中上报 execution_observation    │                                    │
  │ ══════════════════════════════════════>│                                    │
  │ <══════════════════════════════════════ │                                    │
  │   execution_observation_ack             │                                    │
  │                                        │                                    │
  │                                        │  (其他调用方查询状态)                 │
  │                                        │ <────────────请求───────────────────│
  │                                        │ ───────返回缓存execution快照────────>│
  │                                        │                                    │
  │         ... Ping/Pong 心跳持续 ...        │                                    │
  │                                        │                                    │
  │ ⑧ 发送 unregister                        │                                    │
  │ ──────────────────────────────────────>│                                    │
  │ <────────────────────────────────────── │                                    │
  │   unregister_ack {success: true}        │                                    │
  │                                        │                                    │
  │ ⑨ 关闭 WS 连接                           │                                    │
  │ ═══════════════════════════════════════>│                                    │
```


### 异常场景：客户端崩溃

```
客户端                                    注册中心
  │                                        │
  │         ... 正常运行中 ...                │
  │                                        │
  ╳ 客户端崩溃                               │
                                           │
                              Ping ──>  无 Pong 响应  (第1次)
                              Ping ──>  无 Pong 响应  (第2次)
                              Ping ──>  无 Pong 响应  (第3次)
                                           │
                              注册中心关闭 WS 连接
                              移除该连接上所有实例
```

## 附录 A：接入自检清单

### 连接层

- [ ] 能从 Registry Base URL、服务元信息接口或显式配置解析出 `ws_endpoint`
- [ ] 没有任何 Registry 配置时提示用户配置服务端地址
- [ ] 能建立到配置的 `ws_endpoint` 的 WebSocket 连接，并在需要时携带 API Key
- [ ] 只建立一条 WS 连接，所有实例共享

### 注册/注销

- [ ] 实例创建后立即发送 `register` 帧
- [ ] 正确填写 `request_id`、`instance_id`、`role_name`、`description`
- [ ] 能处理 `register_ack` 响应（成功和失败）
- [ ] 实例关闭时发送 `unregister` 帧
- [ ] 重连后重新发送所有实例的 `register`（携带相同 `instance_id`）

### 消息处理

- [ ] 能接收 `message` 帧
- [ ] 收到后 MUST 回复 `ack` 帧（携带对应 `message_id`）
- [ ] 能从 `message` 帧中读取 `to_instance_id`
- [ ] 将消息以 `【{source_tag}】{content}` 格式作为 user message 插入 `to_instance_id` 指定的 Agent 对话

### 状态查询

- [ ] 能在执行状态变化和工具调用时发送 `execution_observation` 帧
- [ ] 能处理 `execution_observation_ack` 响应（成功和失败）

### 心跳与重连

- [ ] WS 库能自动响应 Ping/Pong（或手动实现）
- [ ] WS 断开时启动指数退避重连（1s → 2s → 4s → ... → 30s 封顶）
- [ ] 重连成功后重新注册所有实例

