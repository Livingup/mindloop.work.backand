# LACP - Local Agent Communication Protocol

> 本地/远程 Agent 通信协议规范 v0.4 Draft

---

> **术语说明：** 以下原始需求中的「角色实例」「角色」等术语，在正式协议中统一为「Agent 实例」，详见第2章术语定义。

角色：Agent APP，LACP 客户端，MCP 客户端，MCP 服务端，LACP 注册中心，Agent 角色实例。
目的：为了实现本地Claude code，Codex，manus，ai 浏览器这些agent应用之间能互相沟通交流。
工作方式：
1. Agent APP 中的 LACP 客户端和 MCP 客户端需要配置注册中心端点，本地部署通常使用本机端口，远程部署使用中心服务提供的 HTTPS/WSS 地址。
2. LACP 客户端启动后通过 WebSocket 长连接向注册中心注册自己希望对外暴露的 Agent 实例，并负责管理实例注册状态。注册时需要说明 Agent 功能、能力、定位、工作空间等信息，便于其他 Agent 正确沟通协作。
3.同一个角色可以有多个实例，比如可以注册多个编程agent角色（他们的工作项目不同），第一次注册某个角色实例时，注册中心会返回一个角色实例唯一id。如果 Agent App 支持重启恢复agent角色重新注册这个agent角色时可以在注册时设置这个id，来保证agent角色实例的一致性。这个由 LACP 客户端自己来控制管理。
4.agent角色需要通过 Agent/运行时侧 MCP 客户端连接注册中心提供的 MCP 服务端，发现其他可沟通的agent角色列表，给其他角色发消息或主动获取其他角色的状态。
5. MCP 工具列表包括：1. 获取 Agent 实例详情列表；2. 通过目标实例 ID 发送消息，快速返回投递成功或失败；3. 查询目标实例状态，返回注册中心缓存的执行观察快照；4. 消息内容可以是任务、回复或任意文本，注册中心和 MCP Server 不关心业务内容，只负责鉴权、路由、下发和观察缓存。
6. LACP 客户端需要维护并主动上报 Agent 实例执行观察：状态变化、工具调用轨迹、阶段性工作日志通过 `execution_observation` WebSocket 帧推送到注册中心；`lacp_query_status` 读取注册中心缓存，不再要求注册中心反向实时拉取状态。

7.向某个Agent角色发送消息就是在目标agent角色中对话列表中发送一条user message。格式：[@agentname agentid：*******]消息内容。会在agent角色的系统提示词中说明工作背景：多agent角色沟通协作和这个格式说明以该标记开头的标识表示是一个agent角色发送的消息。

8.agent角色向agent角色发送消息就是通过向目标角色实例对话中插入一条特殊标记的user类型的message。例如对话过程：user massage，assiant message，tool result message，特殊表示的user massage，assiant message，tool result message，assiant message，........

## 1. 协议概述

### 1.1 目的

LACP（Local Agent Communication Protocol）定义了一套支持本地部署和远程中心注册服务部署的多 Agent 通信协议，用于多个独立 Agent 应用之间互相发送消息、发现实例并观察长任务执行状态。

在当前的 AI Agent 生态中，用户本地可能同时运行多个独立的 Agent 产品（如 Claude Code、Manus、AI 浏览器等）。这些 Agent 各自独立运行，各有所长。LACP 的目标是让这些 Agent 之间能够像人与人之间发消息一样，互相沟通、互相协作。

### 1.2 核心理念

**消息即 User Message。**

LACP 的核心设计理念极其简单：Agent 向另一个 Agent 发送消息，本质上就是向目标 Agent 的对话中**插入一条 user message**。

每个 Agent 产品的工作模型都是基于对话的：接收 user message → LLM 处理 → 工具调用 → 继续处理。LACP 不发明新的通信范式，而是直接复用这个已有的机制。来自其他 Agent 的消息，通过特定的 `@标记` 区分来源，Agent 通过系统提示词来理解和处理这些消息。

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **内容无关** | 协议只负责消息的投递，不关心消息内容是什么。消息可以是一句"你好"，也可以是一个复杂的研究请求。 |
| **实例独立** | 每个 Agent 实例完全独立运行，协议不干预也不感知 Agent 的内部行为。 |
| **极简投递** | 没有会话、没有任务、没有编排。发消息就是发消息，仅此而已。 |
| **本地/远程部署** | 协议默认适用于本地开发环境，同时支持远程中心注册服务部署。远程生产部署 SHOULD 使用 HTTPS/WSS。是否使用 API Key 由 Registry 鉴权策略决定，本地和远程客户端都 MUST 遵守该策略。 |

### 1.4 适用范围

- 同一台机器上运行的多个独立 Agent 应用之间的消息通信。
- 多台机器或多工作空间共享同一个远程中心注册服务时的 Agent 消息通信与执行观察。
- 不适用于 Agent 内部的多模块通信。

## 2. 术语与定义

| 术语 | 定义 |
|------|------|
| **Agent 实例（Agent Instance）** | Agent App 内部运行的一个 LLM 驱动的智能体。通过 Agent/运行时侧的 MCP Client 调用 Registry MCP Server 提供的 Tool 来发现其他 Agent、发送消息、查询状态。**Agent 实例不负责自身的注册与注销，这由 LACP 客户端管理。** |
| **LACP 客户端（LACP Client / Agent App Client）** | Agent App 侧的 WebSocket 接入组件。它负责管理 Agent 实例的 LACP 生命周期：启动后与注册中心建立唯一的 WebSocket 长连接，通过该连接发送注册/注销消息，同时通过该连接接收消息推送、回复确认并上报执行观察。一个 LACP 客户端可以接入一个或多个 Agent 实例，所有实例共享同一条 WebSocket 连接。 |
| **实例 ID（Instance ID）** | Agent 实例的唯一标识符，由 LACP 客户端自行生成并维护。实例不变则 ID 不变，保持一致性。 |
| **LACP 注册中心（LACP Registry）** | 一个独立运行的本地或远程中心服务。提供 **WebSocket 端点**供 LACP 客户端建立长连接，通过该连接处理实例注册/注销、消息推送、执行观察事件上报。同时暴露 **MCP Server** 供 Agent/运行时侧 MCP Client 调用。负责消息路由转发、实例状态维护、执行观察缓存、服务元信息发布和 API Key 鉴权。类似电信运营商。 |
| **MCP 服务端（MCP Server）** | 注册中心对外暴露的 MCP Server，供 Agent/运行时侧 MCP Client 调用。提供消息发送、角色发现、状态查询等 Tool。不包含注册/注销操作——那是 LACP 客户端通过 WebSocket 连接完成的。 |
| **MCP 客户端（MCP Client）** | Agent/运行时侧的 MCP 客户端组件，连接 Registry MCP Server 并调用 Tool。MCP Client 不负责实例注册/注销，不接收 Registry 的 WS 推送；这些职责属于 LACP 客户端。 |
| **消息投递（Message Delivery）** | 将一条消息从源 Agent 实例传递到目标 Agent 实例的全链路过程：源 Agent/运行时通过 MCP Client 调用 MCP Tool → MCP 服务端转交注册中心 → 注册中心查找目标实例对应的 WebSocket 连接 → 通过 WS 推送消息至目标 LACP 客户端 → 客户端将消息作为 user message 插入到目标 Agent 实例的对话中。 |
| **来源标记（Source Tag）** | 消息中标识发送方身份的标记，格式为 `@角色名 实例ID`，用于目标 Agent 识别消息来源。 |
| **WebSocket 连接** | LACP 客户端与注册中心之间的**唯一 WS 接入通道**。实例注册/注销、消息推送、执行观察上报、心跳检测均通过此连接完成，无需 LACP 客户端额外启动 HTTP 服务。每个 LACP 客户端与注册中心之间只建立一条 WebSocket 连接。 |

## 3. 协议架构

### 3.1 整体架构

LACP 采用星型架构，LACP Registry 作为中心服务节点。每个 Agent App 可以同时包含两个独立接入组件：LACP 客户端通过 WebSocket 接入 Registry，MCP 客户端通过 MCP 接入 Registry MCP Server。

```
┌──────────────────────────┐          ┌──────────────────────────┐
│        Agent App A        │          │        Agent App B        │
│                          │          │                          │
│  ┌────────────────────┐  │   WS     │  ┌────────────────────┐  │
│  │    LACP Client     │◄═╪══════════╪═►│    LACP Client     │  │
│  └────────────────────┘  │          │  └────────────────────┘  │
│                          │          │                          │
│  ┌────────────────────┐  │   MCP    │  ┌────────────────────┐  │
│  │    MCP Client      │──╪──────────╪──│    MCP Client      │  │
│  └────────────────────┘  │          │  └────────────────────┘  │
│                          │          │                          │
│  ┌────────────────────┐  │          │  ┌────────────────────┐  │
│  │   Agent Instance   │  │          │  │   Agent Instance   │  │
│  └────────────────────┘  │          │  └────────────────────┘  │
└─────────────┬────────────┘          └─────────────┬────────────┘
              │                                     │
              │                                     │
              ▼                                     ▼
        ┌────────────────────────────────────────────────┐
        │                LACP Registry                    │
        │                                                │
        │  - /ws  WebSocket Endpoint                     │
        │  - /mcp MCP Server                             │
        │  - 实例注册表 / 消息路由 / 执行观察缓存          │
        │  - /.well-known/lacp 服务元信息                 │
        └────────────────────────────────────────────────┘
```

### 3.2 角色职责

#### 3.2.1 Agent 实例

- 是 LLM 驱动的智能体，只负责思考和调用工具
- 通过 Agent/运行时侧 MCP Client 调用 MCP Server 提供的 Tool 来发现其他 Agent、发送消息、查询状态
- **不负责自身的注册与注销**，这由 LACP 客户端管理
- 收到来自其他 Agent 的消息（以 `@标记` 开头的 user message）后，通过系统提示词理解并处理

**Agent 实例需要具备的能力（通过系统提示词 + MCP 工具实现）：**

| # | 能力 | 说明 | 实现方式 |
|----|------|------|----------|
| 1 | **来源标记识别** | 能识别对话中以 `【@角色名 实例ID】` 开头的 user message，理解这是来自其他 Agent 的消息而非人类用户的输入。 | 系统提示词 |
| 2 | **Agent 发现** | 需要与其他 Agent 协作时，调用 `lacp_discover` 工具查询当前在线的 Agent 实例列表，了解有哪些 Agent 可以沟通、各自擅长什么。 | MCP Tool |
| 3 | **消息发送** | 调用 `lacp_send_message` 工具向目标 Agent 发送消息，需提供自身 `from_instance_id`、目标 `to_instance_id` 和消息内容 `content`。 | MCP Tool |
| 4 | **状态查询** | 调用 `lacp_query_status` 工具查询指定 Agent 实例的注册状态和最近执行观察快照，用于在发送消息前确认目标是否可达，或在发送消息后跟踪目标的当前状态、摘要和工具调用轨迹。 | MCP Tool |
| 5 | **协作意图理解** | 根据消息内容理解其他 Agent 的请求意图，自主决定如何处理（执行任务、回复结果、拒绝请求等）。协议不干预 Agent 的内部决策。 | LLM 自身能力 |
| 6 | **回复路由** | 处理完其他 Agent 的请求后，能通过 `lacp_send_message` 将结果回复给发送方（从收到的消息中提取 `from_instance_id` 作为回复目标）。 | MCP Tool + 系统提示词 |

> **注意：** Agent 实例本身不负责 LACP 注册/注销，也不接收 Registry 的 WS 推送。主动发现、发送消息和查询状态由 Agent/运行时侧 MCP Client 连接 Registry MCP Server 完成；注册、注销、消息接收和执行观察上报由 LACP 客户端通过 WebSocket 完成。

#### 3.2.2 LACP 客户端（Agent App 侧 WS 接入组件）

- 是 Agent App 侧接入 LACP Registry 的 WebSocket 客户端组件
- **负责管理 Agent 实例的完整生命周期**：
  1. 启动后与注册中心建立**唯一的 WebSocket 长连接**
  2. 创建 Agent 实例（LLM），为其生成唯一的实例 ID
  3. 通过 WebSocket 连接发送**注册消息**将实例注册到注册中心
  4. 实例关闭时，通过 WebSocket 连接发送**注销消息**注销实例
- 一个 LACP 客户端只建立一条 WebSocket 连接，所有实例共享该连接
- 通过 WebSocket 连接接收注册中心推送的消息（**无需自行启动 HTTP 服务**）
- 收到消息后，将其作为 user message 插入到对应 Agent 实例的对话中
- 不负责配置 MCP Server、不管理 MCP Transport、不替 Agent/运行时建立 MCP 会话、不处理工具或提示词集成

**LACP 客户端需要实现的能力：**

| # | 能力 | 说明 | 对应协议章节 |
|----|------|------|------------|
| 1 | **Registry 连接配置** | 启动时 MUST 获得 Registry 服务端连接配置。LACP 客户端 SHOULD 由用户/平台配置 Registry Base URL，再请求该 Base URL 下的 `/.well-known/lacp` 获取 WS 端点路径和鉴权策略；也 MAY 由用户/平台直接提供完整 WS 端点。 | §9.1 |
| 2 | **实例 ID 生成** | 为每个 Agent 实例生成全局唯一的实例 ID。格式自定，协议不约束，但必须保证同一台机器上不会重复。 | §4.1.1 |
| 3 | **实例注册** | 通过 WebSocket 连接发送 `type: "register"` 消息，携带 `instance_id`、`role_name`、`description`。处理注册成功/失败响应。 | §4.1 |
| 4 | **WebSocket 连接建立** | 启动时使用解析后的完整 `ws_endpoint` 发起 WebSocket 连接。LACP 客户端不应写死本地地址；本地和远程只是 URL 形态不同。若鉴权策略要求 API Key，连接握手 MUST 携带 API Key。 | §4.2, §9.2 |
| 5 | **WebSocket 消息监听与确认** | 监听 WebSocket 连接上的 JSON 文本帧。收到 `type: "message"` 的推送消息后，MUST 回复 `type: "ack"` 确认帧（携带对应 `message_id`）。 | §6.2.1, §6.2.2 |
| 6 | **消息插入（核心）** | 收到推送消息后，将其作为一条 user message 插入到目标 Agent 实例的当前对话中。插入格式：`【{source_tag}】{content}`，如 `【@Manus manus-a3f8】帮我测试下这个页面`。 | §6.3 |
| 7 | **执行观察上报** | 在任务状态变化、工具调用开始/完成/失败、阶段性工作日志时，通过 WebSocket 主动发送 `type: "execution_observation"` 帧，供注册中心缓存并服务 `lacp_query_status` 查询。 | §7.4 |
| 8 | **心跳响应** | 响应注册中心通过 WebSocket 发送的 Ping 帧（大多数 WebSocket 客户端库会自动处理 Pong 响应）。 | §4.4 |
| 9 | **实例注销** | 实例关闭时，通过 WebSocket 连接发送 `type: "unregister"` 消息，携带 `instance_id`。所有实例注销完毕后可断开 WebSocket 连接。 | §4.3 |
| 10 | **异常处理与重连** | WebSocket 连接意外断开时，应尝试重新连接。应用崩溃退出时，注册中心会通过心跳超时自动清理，无需客户端额外处理。 | §4.4 |

#### 3.2.3 MCP 客户端（Agent/运行时侧组件）

- 是 Agent/运行时侧连接 Registry MCP Server 的 MCP Client
- 负责按 MCP 协议连接注册中心发布的 MCP Server
- 负责调用 Registry MCP Server 暴露的 Tool，例如发现实例、发送消息、查询状态
- 如果 Registry 鉴权策略要求 API Key，MCP Client 的 MCP 请求 MUST 携带 API Key
- 不负责实例注册/注销，不接收 Registry WS 推送，不上报 `execution_observation`

**MCP 客户端需要实现的能力：**

| # | 能力 | 说明 | 对应协议章节 |
|----|------|------|------------|
| 1 | **MCP Server 连接** | 使用 Registry 服务元信息或显式配置中的 MCP 端点连接 Registry MCP Server。 | §9.3 |
| 2 | **MCP 鉴权** | 当 Registry 鉴权策略要求 API Key 时，为 MCP 请求携带 API Key。 | §9.3 |
| 3 | **Tool 调用** | 按 MCP 协议调用 Registry 暴露的发现、消息发送、状态查询 Tool。 | §5, §6, §7 |

#### 3.2.4 注册中心（Registry）

- 提供 **WebSocket 端点**：供 LACP 客户端建立长连接，通过该连接处理实例的注册与注销，并向客户端推送消息
- 暴露 **MCP Server**：供 Agent/运行时侧 MCP Client 连接并调用 Tool，以发现其他 Agent、发送消息、查询状态
- 维护所有已注册 Agent 实例的信息（角色名、实例 ID、功能介绍、在线状态）
- 维护实例 ID 与 WebSocket 连接的映射关系
- 接收 MCP Server 转交的消息发送请求，通过 WebSocket 连接将消息推送至目标 LACP 客户端
- 通过 WebSocket Ping/Pong 机制检测连接存活，处理异常断开

**注册中心需要实现的能力：**

| # | 能力 | 说明 | 对应协议章节 |
|----|------|------|------------|
| 1 | **服务元信息发布** | Registry MUST 能向接入方提供端点路径和鉴权策略。Registry SHOULD 通过 `/.well-known/lacp` 暴露标准服务元信息；LACP 客户端使用其中的 WS 端点路径，MCP Client 使用其中的 MCP 端点路径。服务元信息不发布 Registry Base URL。 | §9.1 |
| 2 | **WS 消息：注册实例** | 收到客户端发送的 `type: "register"` 消息后，接收 `instance_id`、`role_name`、`description` 参数，将实例写入注册表并标记为 `online`。同一 `instance_id` 重复注册视为更新信息。通过同一 WebSocket 连接返回注册结果。 | §4.1 |
| 3 | **WS 消息：注销实例** | 收到客户端发送的 `type: "unregister"` 消息后，接收 `instance_id` 参数，将实例从注册表中移除。对不存在的 ID 注销返回成功（幂等性）。 | §4.3 |
| 4 | **WebSocket 端点** | 提供 `/ws` 端点，并在服务元信息中发布 WS 端点路径。连接建立后，客户端通过该连接注册/注销、接收消息、上报执行观察。 | §4.2, §7.4 |
| 5 | **注册表维护** | 维护内存中的实例注册表，存储每个实例的 `instance_id`、`role_name`、`description`、`status`、`registered_at` 等信息。维护 `instance_id → WebSocket 连接` 的映射关系。 | §4.1, §7 |
| 6 | **消息路由与推送（核心）** | 收到 MCP Server 转交的消息发送请求后：①查找 `to_instance_id` 对应的实例；②验证实例在线且 WebSocket 可用；③构造推送消息帧（含 `type`、`message_id`、`source_tag`、`from_instance_id`、`from_role_name`、`to_instance_id`、`content`、`timestamp`）；④通过 WebSocket 推送至目标 LACP 客户端；⑤等待确认帧（5秒超时）；⑥返回投递结果。 | §6.1, §6.2 |
| 7 | **消息 ID 生成** | 为每条投递的消息生成唯一的 `message_id`，用于推送消息帧与确认帧的关联。 | §6.2.1 |
| 8 | **来源标记构造** | 根据发送方的 `role_name` 和 `instance_id` 构造 `source_tag`，格式为 `@角色名 实例ID`，如 `@Manus manus-a3f8`。 | §6.2.1 |
| 9 | **发送方验证** | 消息发送时 MUST 验证 `from_instance_id` 已注册，否则拒绝发送并返回 `SEND.SENDER_NOT_REGISTERED` 错误。 | §6.1.3 |
| 10 | **心跳检测** | 定期（建议 30 秒）向所有已建立的 WebSocket 连接发送 Ping 帧。连续 N 次（建议 N=3）未收到 Pong 响应时，关闭连接并将该连接上的实例从注册表中移除，不保留离线状态。 | §4.4 |
| 11 | **WebSocket 断开处理** | 客户端 WebSocket 连接断开时（无论主动关闭还是异常断开），将该连接下所有已注册的实例从注册表中移除，同时使这些实例的执行观察快照失效。 | §4.2.3 |
| 12 | **MCP Server 暴露** | 对外暴露 MCP Server，并在 Registry 服务元信息中发布 MCP 端点路径，提供 `lacp_discover`、`lacp_send_message`、`lacp_query_status` 三个 Tool，供 Agent/运行时侧 MCP Client 调用。 | §9.3 |
| 13 | **错误码返回** | 所有操作失败时 MUST 返回统一格式的错误码（`分类.具体错误`），如 `REGISTER.INVALID_PARAMS`、`SEND.TARGET_NOT_FOUND`、`SEND.DELIVERY_FAILED` 等。 | §8 |

#### 3.2.5 MCP 服务端（MCP Server）

- 是注册中心对外暴露的 MCP Server
- 供 Agent/运行时侧 MCP Client 调用
- 以 MCP Tool 的形式暴露通信操作：发现、发消息、查状态
- **不包含注册/注销操作**——那是 LACP 客户端通过 WebSocket 连接完成的

**MCP Server 需要实现的能力：**

| # | 能力（Tool） | 输入参数 | 输出 | 说明 | 对应协议章节 |
|----|------------|----------|------|------|------------|
| 1 | **`lacp_discover`** | `role_name`（可选，按角色名筛选） | `instances` 数组，每项含 `instance_id`、`role_name`、`description` | 查询当前已注册的 Agent 实例列表。仅返回注册表中存在的实例（均为在线状态），不包含内部连接信息。不传 `role_name` 则返回全部实例。 | §5 |
| 2 | **`lacp_send_message`** | `from_instance_id`（发送方）、`to_instance_id`（目标方）、`content`（消息内容） | `success`、`error_code`（失败时）、`error_message`（失败时） | 向目标 Agent 实例发送消息。MCP Server 收到调用后，将请求转交注册中心进行消息路由与 WebSocket 推送。同步投递、快速返回，不等待目标 Agent 处理结果。 | §6 |
| 3 | **`lacp_query_status`** | `instance_id`（目标实例 ID） | `instance_id`、`role_name`、`status`、`description`、`registered_at`、`execution`（执行观察快照） | 查询指定 Agent 实例的当前注册状态和最近执行观察快照。不存在的 `instance_id` 返回 `QUERY.NOT_FOUND`。注册中心直接读取客户端主动上报的观察缓存，不再通过 WS 反向拉取状态。只读操作。 | §7 |

> **边界说明：** MCP Server 是 Agent/运行时侧 MCP Client 与注册中心之间的工具调用桥梁。它只负责将 MCP Tool 调用转化为对注册中心内部能力的调用，自身不维护状态、不存储数据。注册/注销操作不在 MCP Server 中暴露——那是 LACP 客户端通过 WebSocket 连接直接与注册中心交互完成的程序行为。

### 3.3 接口分层

```
┌──────────────────────────────────────────────────────────────────┐
│                      注册中心 (Registry)                          │
│                                                                │
│  ┌──────────────────────────────┐  ┌──────────────────┐          │
│  │       WebSocket 端点          │  │    MCP Server    │          │
│  │                              │  │                  │          │
│  │  - 实例注册/注销              │  │  - lacp_discover │          │
│  │  - 消息推送                   │  │  - lacp_send_    │          │
│  │  - 执行观察上报                │  │    message       │          │
│  │                              │  │  - lacp_query_   │          │
│  │                              │  │    status        │          │
│  └──────────────┬───────────────┘  └────────┬─────────┘          │
│                 │                           │                    │
└─────────────────┼───────────────────────────┼────────────────────┘
                  │                           │
           WS 长连接                       MCP
        （LACP 客户端）                      │
                  │                           │
            LACP 客户端                   MCP Client
  （注册/注销/接收消息/上报观察）       （发现/发消息/查状态）
```

### 3.4 通信流程概述

```
LACP Client A    MCP Client A      Registry       LACP Client B    Agent B
     │                │              │                 │             │
     │ 1.WS 建连       │              │                 │             │
     │ <════════════════════════════>│                 │             │
     │ 2.WS 注册实例A  │              │                 │             │
     │ ════════════════════════════> │                 │             │
     │ <════════════════════════════ │                 │             │
     │  register_ack                 │                 │             │
     │                │              │                 │             │
     │                │              │ 3.WS 建连        │             │
     │                │              │ <══════════════> │             │
     │                │              │ 4.WS 注册实例B   │             │
     │                │              │ <═══════════════ │             │
     │                │              │ ═══════════════> │             │
     │                │              │                 │             │
     │                │ 5.MCP Tool:  │                 │             │
     │                │ send_message │                 │             │
     │                │ ───────────> │                 │             │
     │                │              │ 6.WS 推送消息    │             │
     │                │              │ ═══════════════>│             │
     │                │              │                 │ 7.插入 user │
     │                │              │                 │ message 到 B│
     │                │              │                 │ ──────────> │
     │                │              │ 8.WS 确认收到    │             │
     │                │              │ <═══════════════│             │
     │                │ 9.返回投递结果│                 │             │
     │                │ <─────────── │                 │             │
```

## 4. Agent 实例注册与注销

### 4.1 注册

LACP 客户端在与注册中心建立 WebSocket 连接后（见 4.2 节），创建 Agent 实例并为其生成唯一实例 ID，然后通过 WebSocket 连接发送**注册消息**。注册成功后，该实例方可被其他 Agent 发现和通信。

#### 4.1.1 注册消息

LACP 客户端通过 WebSocket 连接发送以下 JSON 文本帧进行实例注册：

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 消息类型，固定为 `"register"` 。 |
| `request_id` | string | 是 | 请求唯一标识符，由客户端生成，用于匹配响应。 |
| `instance_id` | string | 是 | Agent 实例的唯一标识符，由 LACP 客户端自行生成并维护。 |
| `role_name` | string | 是 | Agent 角色名称，用于人类可读的标识。如 `"Claude Code"`、`"Manus"`、`"AI Browser"`。 |
| `description` | string | 是 | Agent 实例的功能与作用介绍。用于其他 Agent 发现时了解该实例的能力。如 `"专注于代码编写、调试与工程化的AI编程助手"`。 |
| `workspace` | string | 否 | Agent 实例的工作空间路径或标识。用于区分同一角色的不同实例在做不同项目。如 `"C:/project/my-app"`。 |

示例：

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

#### 4.1.2 注册响应消息

注册中心通过同一 WebSocket 连接返回以下 JSON 文本帧：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息类型，固定为 `"register_ack"` 。 |
| `request_id` | string | 对应注册消息的 `request_id`，用于关联请求与响应。 |
| `success` | boolean | 注册是否成功。 |
| `error_code` | string | 注册失败时的错误码（见第8章）。 |
| `error_message` | string | 注册失败时的错误描述。 |

示例：

```json
{
  "type": "register_ack",
  "request_id": "req-001",
  "success": true
}
```

#### 4.1.3 注册规则

- 同一个 `instance_id` 重复注册视为**更新注册信息**（如更新功能介绍等），而非报错。
- `instance_id` 的格式与生成规则由 LACP 客户端自行决定，协议不做约束，仅要求全局唯一。
- 注册中心 MUST 在注册成功后立即将该实例标记为**在线（online）**状态。

### 4.2 WebSocket 连接建立

LACP 客户端启动后，MUST 首先与注册中心建立 WebSocket 长连接。该连接是 LACP 客户端与注册中心之间的**唯一 WS 接入通道**，所有后续操作（实例注册/注销、消息接收、执行观察上报、心跳）均通过此连接完成。

#### 4.2.1 连接端点

```
<configured ws_endpoint, e.g. ws://127.0.0.1:12589/ws or wss://registry.example.com/ws>
```

- LACP 客户端 MUST 使用 Registry 连接配置中的完整 `ws_endpoint` 建连，不应假设固定本地端口。

#### 4.2.2 连接建立规则

- LACP 客户端启动后 MUST 解析 Registry 连接配置并发起 WebSocket 连接；如果鉴权策略要求 API Key，连接握手 MUST 携带 API Key；连接建立后再通过该连接发送注册消息。
- 一个 LACP 客户端与注册中心之间 MUST 只建立**一条** WebSocket 连接，管理的所有实例共享该连接。
- 注册中心通过消息中的 `instance_id` 字段区分同一连接上的不同实例。

#### 4.2.3 连接断开处理

- WebSocket 连接断开时（无论主动关闭还是异常断开），注册中心 MUST 将该连接上所有已注册的实例从注册表中**移除**。
- 非正常断开（网络异常等）时，注册中心通过 Ping/Pong 机制检测并处理（见 4.4 节）。
- Registry MUST NOT 为已断开或已清理的实例保存离线消息；断开后发往这些实例的 `lacp_send_message` MUST 立即返回失败，调用方可实时获知目标不可达。
- Registry MUST NOT 提供断链后的执行观察事件缓存恢复能力；连接清理后，对这些实例的 `lacp_query_status` MUST 返回 `QUERY.NOT_FOUND`。

### 4.3 注销

LACP 客户端在 Agent 实例关闭时，SHOULD 通过 WebSocket 连接发送**注销消息**。当所有实例注销完毕且不再需要通信时，可断开 WebSocket 连接。

#### 4.3.1 注销消息

LACP 客户端通过 WebSocket 连接发送以下 JSON 文本帧进行实例注销：

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 消息类型，固定为 `"unregister"` 。 |
| `request_id` | string | 是 | 请求唯一标识符，由客户端生成，用于匹配响应。 |
| `instance_id` | string | 是 | 要注销的 Agent 实例 ID。 |

示例：

```json
{
  "type": "unregister",
  "request_id": "req-002",
  "instance_id": "claude-code-001"
}
```

#### 4.3.2 注销响应消息

注册中心通过同一 WebSocket 连接返回以下 JSON 文本帧：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息类型，固定为 `"unregister_ack"` 。 |
| `request_id` | string | 对应注销消息的 `request_id`，用于关联请求与响应。 |
| `success` | boolean | 注销是否成功。 |
| `error_code` | string | 注销失败时的错误码。 |
| `error_message` | string | 注销失败时的错误描述。 |

示例：

```json
{
  "type": "unregister_ack",
  "request_id": "req-002",
  "success": true
}
```

#### 4.3.3 注销规则

- 注销成功后，注册中心 MUST 将该实例从注册表中移除，该实例不再可被发现或接收消息。WebSocket 连接不受影响，同一连接上的其他实例可继续使用。
- 对不存在的 `instance_id` 发起注销，SHOULD 返回成功（幂等性）。

### 4.4 心跳与异常断开检测

由于 LACP 客户端可能异常退出（崩溃、强制终止等），注册中心 MUST 提供基于 WebSocket 的异常断开检测机制：

- 注册中心 MUST 通过 WebSocket 协议标准的 **Ping/Pong 帧** 进行连接存活检测。
- 注册中心 SHOULD 定期（建议间隔 30 秒，可配置）向已建立的 WebSocket 连接发送 Ping 帧。
- 若连续 N 次（建议 N=3）未收到 Pong 响应，注册中心 MUST 关闭该 WebSocket 连接，并将该连接上所有已注册的实例从注册表中**移除**。
- LACP 客户端 MUST 响应来自注册中心的 Ping 帧（大多数 WebSocket 客户端库会自动处理）。

### 4.5 重连策略

LACP 客户端在 WebSocket 连接意外断开时，SHOULD 自动尝试重新连接，并在重连成功后重新注册所有实例。

#### 4.5.1 指数退避重连

- LACP 客户端 SHOULD 采用**指数退避**策略进行重连，避免频繁重连给注册中心造成压力。
- 建议参数：
  - 初始重连等待：**1 秒**
  - 退避倍数：**2 倍**
  - 最大重连间隔：**30 秒**（封顶）
  - 重连次数：**无限制**（持续重试直到连上或 APP 退出）
- 重连延迟计算公式：`delay = min(initialDelay × 2^retryCount, maxDelay)`

#### 4.5.2 重连后重新注册

- 重连成功后，LACP 客户端 MUST 重新发送所有之前已注册实例的 `register` 消息（携带相同的 `instance_id`）。
- 注册中心收到相同 `instance_id` 的注册消息时，视为更新注册信息（见 4.1.3），实例恢复为在线状态。
- 若断连期间注册中心已将实例从注册表中移除（心跳超时），重新注册将作为新注册处理，效果等价。

## 5. Agent 发现

Agent/运行时侧 MCP Client 可以通过 MCP Server 查询当前注册中心中所有可用的 Agent 实例列表，用于了解有哪些 Agent 可以沟通。

### 5.1 发现请求参数

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `role_name` | string | 否 | 按角色名筛选。不传则返回所有已注册的在线实例。 |

### 5.2 发现响应

返回一个 Agent 实例信息列表：

| 字段 | 类型 | 说明 |
|------|------|------|
| `instances` | array | Agent 实例信息数组。 |

每个实例信息包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `instance_id` | string | 实例唯一标识符。 |
| `role_name` | string | 角色名称。 |
| `description` | string | 功能与作用介绍。 |
| `workspace` | string | 工作空间路径或标识（若注册时提供）。 |

### 5.3 发现规则

- 仅返回当前已注册的实例（即注册表中存在的实例，均为 `online` 状态）。
- 已注销或已被清理的实例 MUST NOT 出现在发现结果中。
- 发现结果不包含 `status` 字段，因为返回的实例必然是在线的，无需冗余标记。
- 发现结果不包含实例的内部连接信息，该信息仅注册中心内部使用，不对外暴露。

## 6. 消息投递

消息投递是 LACP 的核心功能。Agent/运行时侧 MCP Client 通过 MCP Server 向注册中心提交消息发送请求，注册中心负责通过 WebSocket 连接将消息推送至目标 LACP 客户端。

### 6.1 发送消息

#### 6.1.1 发送请求参数

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `from_instance_id` | string | 是 | 发送方 Agent 实例 ID。 |
| `to_instance_id` | string | 是 | 目标 Agent 实例 ID。 |
| `content` | string | 是 | 消息内容。协议不关心内容格式，原样投递。 |

#### 6.1.2 发送响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 消息是否成功投递至目标实例。 |
| `error_code` | string | 投递失败时的错误码（见第8章）。 |
| `error_message` | string | 投递失败时的错误描述。 |

#### 6.1.3 发送规则

- 注册中心收到发送请求后，MUST 立即查找 `to_instance_id` 对应的已注册实例。
- 若目标实例存在且在线，注册中心 MUST 查找该实例对应的 WebSocket 连接，并通过该连接推送消息。
- 若目标实例不存在、已注销、已被连接清理或 WebSocket 连接不可用，注册中心 MUST 立即返回失败并给出对应错误码。
- Registry MUST NOT 为目标实例保存离线消息队列；消息投递是实时操作，目标不可达即失败。
- 消息发送是**同步投递、快速返回**的。注册中心通过 WebSocket 推送消息并等待客户端确认帧后返回投递结果，不等待目标 Agent 的处理结果。
- 发送方 `from_instance_id` MUST 是已注册的实例，否则注册中心 SHOULD 拒绝发送。

### 6.2 消息投递格式

注册中心通过 WebSocket 连接向目标 LACP 客户端推送消息时，MUST 使用以下 JSON 格式：

#### 6.2.1 推送消息帧

注册中心通过 WebSocket 向目标客户端发送的消息帧为 JSON 文本帧，格式如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息类型，固定为 `"message"` 。 |
| `message_id` | string | 消息唯一标识符，由注册中心生成，用于确认回复。 |
| `source_tag` | string | 来源标记，格式为 `@角色名 实例ID`。 |
| `from_instance_id` | string | 发送方实例 ID。 |
| `from_role_name` | string | 发送方角色名称。 |
| `from_workspace` | string | 发送方工作目录，可选；用于目标客户端标题展示。若发送方未注册 workspace，则该字段缺省，且 MUST 不拼接进 `content`。 |
| `to_instance_id` | string | 目标实例 ID。LACP 客户端 MUST 使用该字段将消息路由到正确的本地 Agent 实例对话。 |
| `content` | string | 消息内容，原样透传。 |
| `timestamp` | string | 消息发送时间，ISO 8601 格式。 |

示例：

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
  "timestamp": "2025-01-15T10:30:00Z"
}
```

#### 6.2.2 确认帧

LACP 客户端收到推送消息后，MUST 通过同一 WebSocket 连接返回确认帧：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息类型，固定为 `"ack"` 。 |
| `message_id` | string | 对应推送消息的 `message_id`，用于关联确认。 |
| `received` | boolean | 是否成功接收。`true` 表示消息已被接收，并已作为 user message 插入到原推送帧 `to_instance_id` 指定的目标实例对话中。 |

示例：

```json
{
  "type": "ack",
  "message_id": "msg-20250115-a1b2c3",
  "received": true
}
```

#### 6.2.3 投递超时

- 注册中心推送消息后，SHOULD 等待确认帧的超时时间为 **5 秒**（可配置）。
- 超时未收到确认帧，注册中心 SHOULD 视为投递失败，向发送方返回 `SEND.DELIVERY_FAILED` 错误。

### 6.3 消息处理约定

- LACP 客户端收到投递的消息后，MUST 根据推送帧中的 `to_instance_id` 将其作为一条 **user message** 插入到目标 Agent 实例的当前对话中。
- LACP 客户端 MUST 使用 `to_instance_id` 作为实例路由依据，而不是依赖 WebSocket 连接上下文推断目标实例；因为一个客户端可以在同一连接上管理多个实例。
- 插入的 user message SHOULD 以 `source_tag` 作为前缀，使 Agent 能通过系统提示词识别消息来源。
- 消息插入的格式建议为：`【@角色名 实例ID】消息内容`
- Agent 如何理解和处理该消息，属于 Agent 内部行为，协议不做规定。

## 7. 状态查询

Agent/运行时侧 MCP Client 可以通过 MCP Server 查询指定 Agent 实例的当前状态，包括注册信息和实时执行状态。

### 7.1 查询请求参数

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `instance_id` | string | 是 | 要查询的目标 Agent 实例 ID。 |

### 7.2 查询响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `instance_id` | string | 实例唯一标识符。 |
| `role_name` | string | 角色名称。 |
| `status` | string | 连接状态，取值见 7.3。 |
| `description` | string | 功能与作用介绍。 |
| `workspace` | string | 工作空间路径或标识（若注册时提供）。 |
| `registered_at` | string | 注册时间，ISO 8601 格式。 |
| `execution` | object | 实时执行状态（由目标 LACP 客户端上报），见 7.4。 |

### 7.3 连接状态枚举

| 状态值 | 含义 |
|------|------|
| `online` | 实例已注册且在线，可以接收消息。 |

> **说明：** LACP 不存在 `offline` 状态。实例要么在注册表中（`online`），要么不在注册表中（已注销或已被清理）。断连超时或主动注销后，实例直接从注册表中移除，而非标记为离线。

### 7.4 执行观察缓存

执行观察由目标 LACP 客户端主动通过 WebSocket 推送 `execution_observation` 帧到注册中心。注册中心仅在实例在线且仍在注册表中时缓存每个实例最近一次执行快照和最近事件流；收到 `lacp_query_status` MCP 请求时，注册中心 MUST 直接读取缓存并返回，不再通过 WebSocket 反向拉取状态。执行观察缓存是辅助信息，不构成持久状态；实例断开、注销或被清理后，相关执行观察缓存 MUST 失效，Registry MUST NOT 提供断链后的状态事件缓存恢复。

#### 7.4.1 执行观察帧（LACP 客户端 → 注册中心）

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息类型，固定为 `"execution_observation"`。 |
| `request_id` | string | 客户端生成的请求唯一标识，用于匹配确认帧。 |
| `instance_id` | string | 上报执行观察的 Agent 实例 ID。该实例 MUST 已通过同一 WebSocket 连接注册。 |
| `execution_id` | string | 一次任务/执行过程的唯一标识。 |
| `caller_instance_id` | string | 调用方实例 ID，可选。 |
| `event_id` | string | 本次观察事件唯一标识。 |
| `sequence` | number | 同一 `execution_id` 内单调递增的事件序号。 |
| `timestamp` | string | 事件发生时间，ISO 8601 格式。 |
| `state` | string | 当前观察状态：`executing`、`waiting_for_message`、`completed_waiting_caller`。 |
| `event` | object | 本次观察事件，见 7.4.2。 |

示例：

```json
{
  "type": "execution_observation",
  "request_id": "obs-req-001",
  "instance_id": "manus-a3f8",
  "execution_id": "exec-20250115-001",
  "caller_instance_id": "claude-code-001",
  "event_id": "obs-evt-001",
  "sequence": 1,
  "timestamp": "2025-01-15T10:30:00Z",
  "state": "executing",
  "event": {
    "kind": "tool_call",
    "summary": "正在调用浏览器检查页面",
    "tool": {
      "call_id": "tool-001",
      "name": "browser_preview",
      "status": "started",
      "purpose": "打开页面并观察运行状态"
    }
  }
}
```

#### 7.4.2 观察事件对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | string | 事件类型：`state_changed`、`tool_call`、`work_log`。 |
| `summary` | string | 简短状态或事件摘要。 |
| `detail` | string | 更详细的过程说明，可选。 |
| `related_message_id` | string | 关联的 LACP 消息 ID，可选。 |
| `tool` | object | 工具调用观察信息，可选。包含 `call_id`、`name`、`status`、`purpose`、`input_summary`、`output_summary`、`error_message`、`started_at`、`finished_at`、`duration_ms`。 |
| `waiting_for` | object | 当状态为 `waiting_for_message` 时可填写等待对象和原因。 |
| `caller_expected_action` | string | 当状态为 `completed_waiting_caller` 时可提示调用方下一步动作：`new_instruction`、`end_task`、`confirm_or_continue`。 |
| `metadata` | object | 客户端扩展元数据，可选。 |

#### 7.4.3 观察确认帧（注册中心 → LACP 客户端）

注册中心收到 `execution_observation` 后 MUST 返回 `execution_observation_ack`。失败时返回 `OBSERVATION.INVALID_PARAMS`、`OBSERVATION.INSTANCE_NOT_REGISTERED` 或 `OBSERVATION.FORBIDDEN`。

#### 7.4.4 查询返回的 execution 快照

`lacp_query_status` 返回的 `execution` 字段为注册中心缓存的最新快照。若目标实例尚未上报任何执行观察事件，注册中心返回 `state: "unknown"` 和空事件列表。快照包含 `execution_id`、`state`、`current_summary`、`current_detail`、`waiting_for`、`caller_expected_action`、`last_event_id`、`last_sequence`、`started_at`、`updated_at`、`recent_events`、`recent_tool_calls`。

### 7.5 查询规则

- 查询不存在的 `instance_id`，MUST 返回错误码 `QUERY.NOT_FOUND`，表示该实例不存在于注册表中。
- 状态查询是只读操作，不影响任何实例的状态。

## 8. 错误码体系

LACP 定义统一的错误码体系，所有操作（注册、注销、发送、查询）失败时均使用此体系返回错误信息。

### 8.1 错误码格式

错误码为字符串格式，采用 `分类.具体错误` 的命名规则。

### 8.2 错误码列表

#### 8.2.1 注册相关

| 错误码 | 含义 |
|------|------|
| `REGISTER.INVALID_PARAMS` | 注册参数不完整或格式错误。 |

#### 8.2.2 注销相关

| 错误码 | 含义 |
|------|------|
| `UNREGISTER.INVALID_PARAMS` | 注销参数不完整或格式错误。 |

#### 8.2.3 消息投递相关

| 错误码 | 含义 |
|------|------|
| `SEND.TARGET_NOT_FOUND` | 目标 `to_instance_id` 不存在于注册表中（未注册或已被清理）。 |
| `SEND.SENDER_NOT_REGISTERED` | 发送方 `from_instance_id` 未注册，拒绝发送。 |
| `SEND.DELIVERY_FAILED` | 消息通过 WebSocket 推送后未收到确认帧，投递失败。 |
| `SEND.INVALID_PARAMS` | 发送参数不完整或格式错误。 |

#### 8.2.4 查询相关

| 错误码 | 含义 |
|------|------|
| `QUERY.NOT_FOUND` | 查询的 `instance_id` 不存在于注册表中。 |

#### 8.2.5 通用错误

| 错误码 | 含义 |
|------|------|
| `INTERNAL_ERROR` | 注册中心内部错误。 |

## 9. 传输层约定

### 9.1 Registry 服务配置与元信息接口

- Registry MAY 以本地服务形式运行，也 MAY 以远程中心服务形式运行。
- LACP 客户端和 MCP Client 接入 Registry 时，MUST 获得服务配置，而不是假设 Registry 一定在本机固定端口。
- 接入方 SHOULD 支持配置 Registry Base URL，例如 `http://127.0.0.1:12589` 或 `https://registry.example.com`。
- Registry SHOULD 暴露服务元信息接口 `/.well-known/lacp`，用于返回端点路径和鉴权策略。
- LACP 客户端使用 `endpoints.ws` 和 `auth`；MCP Client 使用 `endpoints.mcp` 和 `auth`。两者 MAY 直接使用用户/平台提供的完整端点配置；如果只配置 Base URL，则 SHOULD 请求 `/.well-known/lacp` 获取标准服务元信息，并基于已配置的 Base URL 解析相对端点路径。
- 本地部署的默认监听地址 MAY 是 `127.0.0.1`，默认端口 MAY 是 `12589`，但接入方不应把这些值作为唯一入口写死。
- 远程部署 SHOULD 在反向代理或网关层暴露同一组标准路径，并使用 HTTPS/WSS 终止 TLS。
- 是否需要 API Key 只由 Registry 配置中的 `auth.required` 决定；与本地/远程部署形态无关。生产远程部署 SHOULD 设置 `auth.required = true`。

#### 9.1.1 Registry 服务配置文件

Registry 服务端 SHOULD 支持 JSON 配置文件。推荐文件名：`lacp.registry.json`。服务端配置文件只用于启动和部署 Registry，不是 LACP 客户端配置文件。

配置文件示例：

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 12589,
  "ws_path": "/ws",
  "mcp_path": "/mcp",
  "heartbeat_interval_ms": 30000,
  "max_missed_pongs": 3,
  "ack_timeout_ms": 5000,
  "auth": {
    "required": false,
    "header": "authorization",
    "api_keys": []
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
|----|----|----|
| `listen_host` | string | Registry 服务进程监听地址。本地部署通常为 `127.0.0.1`；容器或远程部署通常为 `0.0.0.0`。 |
| `listen_port` | number | Registry 服务进程监听端口。 |
| `ws_path` | string | WebSocket 端点路径，默认 `/ws`。 |
| `mcp_path` | string | MCP Server 端点路径，默认 `/mcp`。 |
| `heartbeat_interval_ms` | number | Registry 向 LACP 客户端发送 Ping 的间隔。 |
| `max_missed_pongs` | number | 连接被判定异常前允许连续漏掉 Pong 的次数。 |
| `ack_timeout_ms` | number | Registry 推送消息后等待 `ack` 的超时时间。 |
| `auth.required` | boolean | 是否要求 API Key。这是鉴权的唯一开关。 |
| `auth.header` | string | API Key 请求头，默认 `authorization`。 |
| `auth.api_keys` | string[] | `auth.required = true` 时允许访问 Registry 的 API Key 列表。 |

配置合并优先级 SHOULD 为：默认值 < 配置文件 < 环境变量 < CLI 参数。API Key 等敏感信息 SHOULD 通过环境变量或密钥管理服务注入，不应提交到 Git。

#### 9.1.2 服务部署方式

Registry 是独立服务，部署方式包括：

| 模式 | 说明 |
|----|----|
| 本地部署 | Registry 监听本机地址，适合单机多个 Agent App 互通。默认端点为 `http://127.0.0.1:12589`。 |
| 远程部署 | Registry 作为中心服务运行在服务器、容器或内网服务中，客户端通过 `https://...` / `wss://...` 访问。 |

远程部署 SHOULD 满足：

- 通过反向代理或网关终止 TLS，暴露 HTTPS/WSS。
- 反向代理 MUST 支持 `/ws` WebSocket Upgrade。
- 反向代理 MUST 透传 MCP HTTP 请求体和 API Key 请求头。
- 生产环境 SHOULD 启用 API Key。

完整部署指南见 `LACP-registry-deployment.md`。

#### 9.1.3 服务元信息响应

`GET /.well-known/lacp` SHOULD 返回 JSON：

```json
{
  "name": "LACP Registry",
  "status": "running",
  "endpoints": {
    "ws": "/ws",
    "mcp": "/mcp",
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

#### 9.1.4 客户端配置对象示例

```json
{
  "base_url": "https://registry.example.com",
  "api_key": "<由用户或平台安全注入，不应硬编码>"
}
```

### 9.2 WebSocket 端点（供 LACP 客户端连接）

注册中心 MUST 提供 WebSocket 端点，作为 LACP 客户端与注册中心之间的**唯一 WS 接入通道**：

- 端点路径：`/ws`。
- `ws_endpoint` 示例：`ws://127.0.0.1:12589/ws` 或 `wss://registry.example.com/ws`。
- LACP 客户端 MUST 使用 Registry 连接配置提供的完整 `ws_endpoint`，不应自行根据本地/远程拼接固定地址。
- 如果 Registry 的鉴权策略要求 API Key，LACP 客户端 MUST 在连接请求中携带 API Key。推荐使用 `Authorization: Bearer <api-key>`；无法设置请求头的客户端 MAY 使用服务端明确支持的替代方式，例如 `api_key` 查询参数。是否需要 API Key 由 Registry 配置决定，不能仅按本地/远程判断。
- 每个 LACP 客户端与注册中心之间只建立**一条** WebSocket 连接。
- 连接建立后，LACP 客户端通过此连接进行实例注册/注销（见第 4 章）。
- 注册中心通过此连接向客户端推送消息（见第 6.2 节）。
- LACP 客户端通过此连接主动上报执行观察（见第 7.4 节）。
- 注册中心通过此连接进行 Ping/Pong 存活检测（见第 4.4 节）。
- **LACP 客户端无需自行启动 HTTP 服务**，所有 WS 接入交互均通过此 WebSocket 连接完成。

### 9.3 MCP Server（供 MCP Client 调用）

- 注册中心 MUST 对外暴露一个 MCP Server。本地部署通常使用 HTTP，远程部署 SHOULD 使用 HTTPS。
- Agent/运行时侧 MCP Client 通过连接此 MCP Server 获得通信能力；如果 Registry 的鉴权策略要求 API Key，MCP 请求 MUST 携带 API Key。
- MCP Server 提供以下 Tools：

| Tool 名称 | 对应操作 | 说明 |
|------|------|------|
| `lacp_discover` | 发现（第5章） | 查询当前可用的 Agent 实例列表。 |
| `lacp_send_message` | 消息投递（第6章） | 向目标 Agent 实例发送消息。 |
| `lacp_query_status` | 状态查询（第7章） | 查询指定 Agent 实例的注册信息和最近执行观察快照。 |

- **注意：MCP Server 不包含注册/注销操作。** 注册/注销是 LACP 客户端通过 WebSocket 连接完成的程序行为，不是 LLM 的工具调用。

## 附录

### 附录 A：完整通信示例

以下展示三个 Agent（Claude Code、Manus、AI Browser）之间的完整通信过程。

**前置条件：注册中心服务已启动，LACP 客户端和 MCP Client 已配置 Registry Base URL 并可获取 `/.well-known/lacp` 服务元信息。**

#### 步骤 1：各 LACP 客户端创建实例、注册并建立 WS 连接

```
--- Claude Code ---

1) Claude Code 的 LACP 客户端与注册中心建立 WebSocket 长连接:
   ws://127.0.0.1:12589/ws
   连接建立成功

2) Claude Code 的 LACP 客户端创建 Agent 实例（LLM），生成实例 ID "claude-code-001"

3) 通过 WebSocket 连接注册实例:
   发送: {
     "type": "register",
     "request_id": "req-001",
     "instance_id": "claude-code-001",
     "role_name": "Claude Code",
     "description": "专注于代码编写、调试与工程化的AI编程助手"
   }
   收到: {"type": "register_ack", "request_id": "req-001", "success": true}

--- Manus ---

1) Manus 的 LACP 客户端与注册中心建立 WebSocket 长连接:
   ws://127.0.0.1:12589/ws
   连接建立成功

2) Manus 的 LACP 客户端创建 Agent 实例（LLM），生成实例 ID "manus-a3f8"

3) 通过 WebSocket 连接注册实例:
   发送: {
     "type": "register",
     "request_id": "req-001",
     "instance_id": "manus-a3f8",
     "role_name": "Manus",
     "description": "擅长深度研究、资料整理与长文报告生成的AI助手"
   }
   收到: {"type": "register_ack", "request_id": "req-001", "success": true}

--- AI Browser ---

1) AI Browser 的 LACP 客户端与注册中心建立 WebSocket 长连接:
   ws://127.0.0.1:12589/ws
   连接建立成功

2) AI Browser 的 LACP 客户端创建 Agent 实例（LLM），生成实例 ID "ai-browser-7x"

3) 通过 WebSocket 连接注册实例:
   发送: {
     "type": "register",
     "request_id": "req-001",
     "instance_id": "ai-browser-7x",
     "role_name": "AI Browser",
     "description": "具备网页浏览、前端测试、截图能力的AI浏览器"
   }
   收到: {"type": "register_ack", "request_id": "req-001", "success": true}
```

#### 步骤 2：Claude Code 实例（LLM）发现可用 Agent

```
Claude Code 的 MCP Client 调用 MCP Tool lacp_discover:
  role_name: (不传，查询全部)

返回:
  instances:
    - instance_id: "manus-a3f8", role_name: "Manus", status: "online", ...
    - instance_id: "ai-browser-7x", role_name: "AI Browser", status: "online", ...
```

#### 步骤 3：Claude Code 实例（LLM）向 Manus 发送消息

```
Claude Code 的 MCP Client 调用 MCP Tool lacp_send_message:
  from_instance_id: "claude-code-001"
  to_instance_id: "manus-a3f8"
  content: "帮我研究下什么是MCP协议，给出一份详细的技术报告"

注册中心处理:
  1. 查找 "manus-a3f8" → 在线，WebSocket 连接可用
  2. 通过 WebSocket 推送消息帧至 Manus 的 LACP 客户端:
     {
       "type": "message",
       "message_id": "msg-20250115-x1y2z3",
       "source_tag": "@Claude Code claude-code-001",
       "from_instance_id": "claude-code-001",
       "from_role_name": "Claude Code",
       "to_instance_id": "manus-a3f8",
       "content": "帮我研究下什么是MCP协议，给出一份详细的技术报告",
       "timestamp": "2025-01-15T10:30:00Z"
     }
  3. Manus 的 LACP 客户端通过 WebSocket 返回确认帧:
     {"type": "ack", "message_id": "msg-20250115-x1y2z3", "received": true}
  4. 注册中心返回 Claude Code: {"success": true}

Manus 的 LACP 客户端内部:
  收到 WS 推送消息后，Manus 的 LACP 客户端根据 `to_instance_id = manus-a3f8` 将消息插入到 Manus 实例的对话中:
  【@Claude Code claude-code-001】帮我研究下什么是MCP协议，给出一份详细的技术报告
  Manus 开始独立处理该消息...
```

#### 步骤 4：Manus 实例（LLM）完成研究后回复 Claude Code

```
Manus 的 MCP Client 调用 MCP Tool lacp_send_message:
  from_instance_id: "manus-a3f8"
  to_instance_id: "claude-code-001"
  content: "MCP协议研究报告：\n1. MCP全称Model Context Protocol...（详细内容）"

注册中心处理:
  1. 查找 "claude-code-001" → 在线，WebSocket 连接可用
  2. 通过 WebSocket 推送消息帧至 Claude Code 的 LACP 客户端
  3. Claude Code 的 LACP 客户端返回确认帧: {"type": "ack", ...}
  4. 注册中心返回 Manus: {"success": true}

Claude Code 的 LACP 客户端内部:
  收到 WS 推送消息后，Claude Code 的 LACP 客户端插入一条 user message 到 Claude Code 实例的对话中:
  【@Manus manus-a3f8】MCP协议研究报告：\n1. MCP全称Model Context Protocol...（详细内容）
  Claude Code 实例（LLM）识别到这是来自 Manus 的回复，继续后续工作...
```

### 附录 B：Agent 系统提示词建议

为使 Agent 能正确理解和处理来自其他 Agent 的消息，Agent 产品或运行时 SHOULD 通过系统提示词或等效机制加入类似以下说明（其中 `{instance_id}` 和 `{role_name}` 由 Agent 产品或运行时动态替换）：

```
你当前运行在 LACP（Local Agent Communication Protocol）环境中，本机还有其他 Agent 在独立运行。
你的角色名称是：{role_name}
你的实例 ID 是：{instance_id}
当你调用 lacp_send_message 工具时，请使用上述实例 ID 作为 from_instance_id。
你可能会收到来自其他 Agent 的消息，这些消息以【@角色名 实例ID】开头。
这些消息与用户消息一样需要你理解和处理。
你可以通过 LACP MCP 工具向其他 Agent 发送消息或查询其状态。
回复其他 Agent 时，请使用 lacp_send_message 工具。
```

> **集成建议：** Agent 产品或运行时 SHOULD 通过系统提示词或等效机制向 Agent 实例提供其 `instance_id`，否则 Agent 无法正确填写 `lacp_send_message` 的 `from_instance_id` 参数。该提示词集成属于 Agent/运行时侧能力，不属于 LACP 客户端的 WS 接入职责。

### 附录 C：协议变更记录

| 版本 | 日期 | 变更说明 |
|------|------|------|
| 0.1 Draft | 2025-01-15 | 初始草案。定义核心架构、注册/注销、发现、消息投递、状态查询、错误码体系。 |
| 0.2 Draft | 2025-01-15 | 架构重构：消息接收从 HTTP 回调改为 WebSocket 长连接推送；明确先创建实例后注册的生命周期顺序；新增 WS 连接建立规范、推送消息帧与确认帧格式、WS 端点约定；心跳改为 Ping/Pong；移除 message_callback 字段；新增 SEND.TARGET_UNREACHABLE 错误码。 |
| 0.3 Draft | 2025-01-15 | 统一为纯 WebSocket 架构：移除所有 HTTP API（注册/注销改为 WS 消息 `register`/`unregister`）；每个 Agent App 客户端只建一条 WS 连接，所有实例共享；WS 连接端点去掉 `instance_id` 查询参数；新增 `request_id` 字段用于请求-响应配对；移除 `reachable` 中间状态和 `offline` 状态（断连超时直接删除实例）；移除 `SEND.TARGET_OFFLINE`、`SEND.TARGET_UNREACHABLE` 错误码，新增 `QUERY.NOT_FOUND` 错误码；重写 §7 状态查询，新增执行状态查询能力（`query_status`/`query_status_ack` WS 帧，`execution` 对象含 `state`/`progress`/`description`）；注册消息新增可选 `workspace` 字段；discover 响应移除冗余 `status` 字段、新增 `workspace` 字段；附录 B 系统提示词新增 `instance_id` 和 `role_name` 动态注入要求；新增 §4.5 重连策略规范（指数退避、重连后重注册）；移除 `http_base_url` 配置项；删除 §9.2 HTTP API 章节，原 §9.4 WS 端点前移为新 §9.2；更新附录 A 通信示例；新增术语对照说明。 |
| 0.4 Draft | 2026-05-22 | 支持本地/远程注册中心服务部署；新增 API Key 鉴权与 `/.well-known/lacp` 服务元信息接口；将状态查询从 WS 反向拉取改为客户端主动上报 `execution_observation`，注册中心缓存执行观察快照并由 `lacp_query_status` 读取；新增观察状态、工具调用事件流、观察确认帧和鉴权/观察相关错误码。 |
