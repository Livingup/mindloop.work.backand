# A2A 客户端调用服务端 Agent 的实战生命周期指南

本文档从一个 A2A Client 调用远端 A2A Server Agent 的视角，按实际运行过程描述 A2A 协议中的连接确认、Agent 发现、任务启动、任务暂停、任务恢复、追加提示、普通文本响应、结果返回与收尾处理。

本文档面向 AgentRuntime 后续接入 A2A 时的实现设计，不是对官方规范的逐字段翻译。官方协议以 A2A v1.0 规范和 `specification/a2a.proto` 为准。

## 1. 官方资料与协议基线

- A2A 官方文档：https://a2a-protocol.org/latest/
- A2A 最新规范：https://a2a-protocol.org/latest/specification/
- A2A GitHub 仓库：https://github.com/a2aproject/A2A
- A2A 规范性 Proto：https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto

A2A v1.0 的核心事实：

- A2A 是 Agent-to-Agent 通信协议。
- A2A Server 对外暴露 Agent 能力。
- A2A Client 发现并调用远端 Agent。
- AgentCard 描述 Agent 身份、能力、协议入口、安全要求与技能。
- Task 是有生命周期的工作单元。
- Message 是通信回合。
- Part 是 Message 或 Artifact 中的内容片段。
- Artifact 是任务产物，任务结果应优先通过 Artifact 返回，而不是塞进普通 Message。
- Streaming 使用 SSE。
- 异步长任务可以通过 polling、streaming 或 push notification 获取状态。

## 2. 参与方与最小拓扑

从客户端实战角度，最小拓扑是：

```text
A2A Client
  -> 发现 AgentCard
  -> 选择协议 binding 和 endpoint
  -> 认证
  -> SendMessage / SendStreamingMessage
  -> GetTask / SubscribeToTask / CancelTask
  -> 获取 Artifact 结果

A2A Server Agent
  -> 暴露 AgentCard
  -> 接收 Message
  -> 创建或更新 Task
  -> 执行内部 Agent / LLM / Tool loop
  -> 返回 Task、Message、StatusUpdate、ArtifactUpdate
```

对 AgentRuntime 来说，接入 A2A 不应只做服务端。AgentRuntime 应同时具备：

- A2A Server：让外部 Agent 调用本 Runtime 内的 Agent。
- A2A Client：让本 Runtime 内的 Agent 调用外部 A2A Agent。
- A2A Adapter：把外部 A2A Agent 映射为本 Runtime 内部可调度能力。

## 3. 生命周期总览

一次客户端调用远端 Agent 的完整实战流程可以按以下顺序理解：

```text
1. 确定远端地址来源
2. 建立基础网络连接并确认协议版本
3. 获取 AgentCard
4. 校验 AgentCard、认证方式、能力和协议 binding
5. 选择调用方式：同步、异步、流式、push notification
6. 发送初始 Message，创建或触发 Task
7. 接收 Task / Message 响应
8. 在任务运行中获取状态更新
9. 在 input-required / auth-required 状态下暂停等待客户端输入
10. 客户端用相同 taskId/contextId 恢复任务
11. 客户端在任务未终结时继续发送新的提示消息
12. 处理服务端 Agent 返回普通文本但无工具调用的情况
13. 任务进入 terminal state
14. 客户端读取 artifacts、history 和最终状态
15. 客户端执行收尾处理
```

## 4. 第一步：确定远端地址来源

A2A 不强制中心化 Agent 注册中心。客户端可以通过多种方式拿到远端 Agent 地址。

### 4.1 直接配置 AgentCard URL

最实用的第一阶段方式是直接配置 AgentCard URL：

```yaml
a2a:
  remote_agents:
    - name: research-agent
      agent_card_url: "https://research.example.com/.well-known/agent-card.json"
```

客户端启动后读取配置，直接拉取该 AgentCard。

### 4.2 Well-Known URI 发现

公开 Agent 推荐使用 well-known 路径：

```text
GET https://{agent-server-domain}/.well-known/agent-card.json
```

例如：

```text
GET https://research.example.com/.well-known/agent-card.json
```

### 4.3 私有 Registry 或 Catalog

企业内部可以有私有 registry：

```text
GET https://agents.company.com/catalog
```

registry 返回多个 AgentCard 或 AgentCard URL。A2A 允许这种模式，但它不是协议必需组件。

### 4.4 本地 Agent 的特殊情况

本地 Claude Code、Codex CLI 等没有公网暴露能力时，本文档只按 localhost A2A Server 处理，不考虑通过公网入口暴露给远端 Agent 调用。

- 作为 localhost A2A Server，仅供本机 AgentRuntime、IDE 或本机 orchestrator 调用。
- 被 AgentRuntime 包装成 localhost A2A Server 背后的本地执行器。
- 不设计通过 tunnel、gateway、relay 或公网域名把本地 Agent 暴露给远端。

## 5. 第二步：建立连接与协议确认

A2A 的“建立连接”不是一个单独的握手 RPC。实战上由以下动作组成：

```text
1. 建立 HTTPS / TLS 连接
2. 获取 AgentCard
3. 选择 supportedInterfaces 中的协议 binding
4. 每次请求携带 A2A-Version
5. 按 AgentCard.securitySchemes 获取并发送认证凭据
```

### 5.1 TLS 与服务端身份

生产环境应使用 HTTPS 或 gRPC TLS。客户端应校验证书，确认服务端身份。

### 5.2 协议版本

客户端每次请求应发送：

```http
A2A-Version: 1.0
```

如果服务端不支持该版本，应返回 `VersionNotSupportedError`。

### 5.3 选择协议 Binding

AgentCard 的 `supportedInterfaces` 会声明可用 endpoint：

```json
{
  "supportedInterfaces": [
    {
      "url": "https://agent.example.com/a2a/v1",
      "protocolBinding": "HTTP+JSON",
      "protocolVersion": "1.0"
    },
    {
      "url": "https://agent.example.com/rpc",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    }
  ]
}
```

客户端应选择自己支持且协议版本匹配的 interface。第一版实现建议优先支持 `HTTP+JSON` 或 `JSONRPC`，后续再支持 gRPC。

## 6. 第三步：Agent 发现与能力校验

客户端获取 AgentCard 后，需要做这些检查：

```text
1. Agent 身份：name、description、provider、version
2. 协议入口：supportedInterfaces
3. 输入输出模式：defaultInputModes、defaultOutputModes、skill.inputModes、skill.outputModes
4. 技能：skills
5. 能力：capabilities.streaming、capabilities.pushNotifications、capabilities.extendedAgentCard
6. 安全：securitySchemes、securityRequirements
7. 扩展：capabilities.extensions
8. 签名：signatures，如果启用可信 AgentCard 校验
```

### 6.1 选择 Skill

A2A 没有强制要求 SendMessage 必须显式传 `skillId`。通常客户端根据 AgentCard 的 `skills` 判断这个 Agent 是否适合当前任务，然后把意图表达在 Message 中。具体 skill 路由由服务端 Agent 自己处理。

客户端本地可以维护一个远端能力索引：

```text
remote_agent_id
  name
  description
  skills[]
  input_modes[]
  output_modes[]
  supports_streaming
  supports_push_notifications
  auth_requirement
```

### 6.2 能力前置校验

客户端在调用前应检查：

- 如果要使用 `SendStreamingMessage` 或 `SubscribeToTask`，要求 `capabilities.streaming == true`。
- 如果要注册 webhook，要求 `capabilities.pushNotifications == true`。
- 如果要调用 `GetExtendedAgentCard`，要求 `capabilities.extendedAgentCard == true`。
- 如果消息里包含文件、图片、JSON 等内容，应确认对应 media type 被 Agent 或 Skill 支持。

如果客户端忽略这些检查，服务端必须返回相应错误，如 `UnsupportedOperationError`、`PushNotificationNotSupportedError`、`ContentTypeNotSupportedError`。

## 7. 第四步：认证

AgentCard 会声明认证方式。常见方式包括：

- API Key
- HTTP Bearer
- OAuth 2.0
- OpenID Connect
- mTLS

客户端流程：

```text
1. 读取 AgentCard.securitySchemes
2. 选择满足 securityRequirements 的认证方案
3. 通过 out-of-band 方式获取凭据
4. 每次 A2A 请求携带认证信息
```

例如 Bearer：

```http
Authorization: Bearer <access_token>
A2A-Version: 1.0
```

OAuth Device Code 对 CLI Agent 较重要，因为本地 CLI 可以让用户在浏览器完成授权，再回到 CLI 继续调用。

## 8. 第五步：选择调用模式

A2A 客户端启动任务前需要决定采用哪种交互模式。

### 8.1 普通请求响应

适合短任务：

```text
SendMessage -> 返回 Message 或 Task
```

如果返回 Task，客户端可用 `GetTask` 轮询。

### 8.2 流式调用

适合交互式长任务：

```text
SendStreamingMessage -> SSE stream
```

服务端通过 SSE 发送：

- `Task`
- `Message`
- `TaskStatusUpdateEvent`
- `TaskArtifactUpdateEvent`

### 8.3 后续订阅已有任务

如果 SSE 中断但任务未结束：

```text
SubscribeToTask(taskId) -> SSE stream
```

如果任务已是终态，服务端应返回 `UnsupportedOperationError`。

### 8.4 Webhook Push Notification

适合长时间任务和客户端不保持连接的场景。客户端通过初始请求或后续接口注册 webhook：

```text
CreateTaskPushNotificationConfig
```

服务端在关键状态变化时 POST `StreamResponse` 到客户端 webhook。

## 9. 第六步：发送初始 Message

### 9.1 HTTP+JSON 示例

```http
POST /message:send HTTP/1.1
Host: agent.example.com
Content-Type: application/a2a+json
A2A-Version: 1.0
Authorization: Bearer <token>
```

```json
{
  "message": {
    "messageId": "msg-user-001",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "请分析这个仓库并生成架构报告。",
        "mediaType": "text/plain"
      }
    ]
  },
  "configuration": {
    "acceptedOutputModes": ["text/plain", "application/json"],
    "historyLength": 20,
    "returnImmediately": false
  }
}
```

### 9.2 JSON-RPC 示例

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-user-001",
      "role": "ROLE_USER",
      "parts": [
        {
          "text": "请分析这个仓库并生成架构报告。",
          "mediaType": "text/plain"
        }
      ]
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain", "application/json"],
      "historyLength": 20,
      "returnImmediately": false
    }
  }
}
```

### 9.3 `returnImmediately` 的影响

`SendMessageConfiguration.returnImmediately` 是客户端控制响应时机的重要字段。

- `true`：服务端创建任务后可以立即返回，即使任务仍在执行。
- `false`：服务端应等待任务进入终态或中断态再返回。

终态：

- `TASK_STATE_COMPLETED`
- `TASK_STATE_FAILED`
- `TASK_STATE_CANCELED`
- `TASK_STATE_REJECTED`

中断态：

- `TASK_STATE_INPUT_REQUIRED`
- `TASK_STATE_AUTH_REQUIRED`

第一版客户端实现建议：

- UI 交互任务优先使用 `SendStreamingMessage`。
- 非流式长任务设置 `returnImmediately: true`，再用 `GetTask` 轮询。
- 简短任务可以设置 `returnImmediately: false`。

## 10. 第七步：处理初始响应

`SendMessageResponse` 是 oneof：

```text
SendMessageResponse
  task: Task
  message: Message
```

### 10.1 返回 Message

服务端可能直接返回 Message，适合简单问答、能力协商或拒绝创建任务前的澄清。

客户端处理方式：

```text
1. 展示 agent message
2. 记录 contextId，如果响应中有
3. 如果用户继续追问，用相同 contextId 再发 SendMessage
4. 不要假设一定有 taskId
```

### 10.2 返回 Task

服务端创建或更新 Task 时返回 Task。

客户端需要保存：

```text
task.id
contextId
status.state
status.message
artifacts
history
metadata
```

后续所有与该任务相关的操作都依赖 `taskId` 和 `contextId`。

### 10.3 `contextId` 是什么，服务端什么时候返回

`contextId` 是 A2A 用来表示“连续上下文”的标识。它把多条相关 Message 和多个相关 Task 归到同一组交互里，类似一次跨任务的会话上下文。

它不是：

- 客户端 ID。
- 租户 ID。
- 任务 ID。
- 网络连接 ID。
- HTTP session ID。

它表达的是：

```text
这些 Message / Task 属于同一段连续业务上下文
```

例如：

```text
contextId = ctx-abc
  task-123：生成第一版架构报告
  task-456：基于第一版架构报告优化性能分析
  task-789：基于优化结果生成迁移计划
```

#### 10.3.1 谁生成 `contextId`

A2A 允许服务端生成 `contextId`。实战中推荐由服务端生成，并由客户端保存和复用。

首次请求时，客户端通常不传 `contextId`：

```json
{
  "message": {
    "messageId": "msg-user-001",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "请生成架构报告。"
      }
    ]
  }
}
```

服务端创建上下文后，在响应里返回：

```json
{
  "task": {
    "id": "task-123",
    "contextId": "ctx-abc",
    "status": {
      "state": "TASK_STATE_WORKING"
    }
  }
}
```

如果服务端直接返回 Message，也可以在 Message 里返回 `contextId`：

```json
{
  "message": {
    "messageId": "msg-agent-001",
    "contextId": "ctx-abc",
    "role": "ROLE_AGENT",
    "parts": [
      {
        "text": "我需要更多信息才能开始。"
      }
    ]
  }
}
```

#### 10.3.2 客户端能不能自己传 `contextId`

客户端可以传 `contextId`，但不应随意自己生成。更安全的规则是：

```text
第一次请求：不传 contextId，等服务端生成。
后续请求：复用服务端返回的 contextId。
```

如果客户端传了服务端不接受的 `contextId`，服务端应拒绝请求，而不是悄悄换成另一个上下文。

服务端生成的 `contextId` 对客户端来说应视为不透明字符串。客户端只保存、传回、关联本地 UI 和任务，不解析其中含义。

#### 10.3.3 服务端会维护 `contextId` 对应的状态吗

有状态 Agent Server 通常会用 `contextId` 维护：

- 对话历史。
- 任务历史。
- LLM 上下文。
- 已生成 artifact 的索引。
- 用户输入和中间决策。
- 服务端内部执行状态。

但 A2A 不强制服务端永久保存这些状态。服务端可以有过期、清理、容量限制和权限隔离策略。客户端不能假设 `contextId` 永远有效。

因此客户端应同时保存：

```text
contextId
taskId
最终 artifacts
关键 history
本地会话记录
```

如果后续继续优化时服务端上下文已过期，客户端应把上一次结果的关键内容重新作为新 Message 输入。

#### 10.3.4 后续请求怎么使用 `contextId`

如果继续一个未终结任务，客户端发送：

```text
taskId + contextId
```

如果基于已完成任务继续做新任务，客户端发送：

```text
contextId + referenceTaskIds
```

如果只传 `taskId`，服务端应能从任务推导出对应 `contextId`。如果客户端同时传了 `taskId` 和不匹配的 `contextId`，服务端应拒绝请求。

### 10.4 多客户端并发调用同一 Agent Server 时怎么区分

同一个 AgentCard 或同一个 A2A Server 定义可以被多个客户端同时调用，也可以被同一个客户端同时发起多个任务。AgentCard 只说明“这个服务端 Agent 是谁、会什么、从哪里调用”，不代表某一次具体调用。

客户端区分并发调用时，核心依赖：

```text
remote_agent_id / interface_url：区分调用的是哪个远端 Agent 服务
messageId：区分客户端发出的某一条消息，也用于重试幂等
taskId：区分服务端创建的某一个具体任务
contextId：区分一组相关任务和消息所属的连续上下文
auth account / local workspace：客户端本地的账号、工作区或权限隔离维度
```

服务端每创建一个新 Task，都必须返回该 Task 的 `id`。客户端应把 `taskId` 当作服务端作用域内的不透明 ID，不要假设它在所有远端 Agent 之间全局唯一。

推荐客户端本地映射键：

```text
remote_task_key = interface_url + taskId
```

如果同一个 Agent Server 会被多个账号、多个工作区或多个本地 Agent 实例同时调用，客户端可以扩展为：

```text
remote_task_key = local_workspace_id + local_agent_instance_id + interface_url + taskId
```

收到 streaming event、polling 结果或 artifact chunk 时，客户端都应按 `interface_url + taskId` 路由到本地对应任务 UI、状态机和 artifact 缓冲区。

### 10.5 `tenant` 是什么，A2A 会返回吗

`tenant` 不是 A2A v1.0 标准要求返回的字段，也不是 Task 续接机制的一部分。它通常是客户端平台或服务端平台自己的多租户隔离概念，例如：

- 本地工作区 ID。
- 用户账号 ID。
- 组织 ID。
- 项目 ID。
- API Key 所属主体。
- OAuth token claim 中的 `sub`、`org_id`、`tenant_id` 等自定义声明。
- 服务端自定义 URL、Header 或 metadata 中携带的租户维度。

A2A 标准响应里，客户端应该稳定依赖的是：

```text
Task.id
Task.contextId
Task.status
Task.artifacts
Message.messageId
Message.contextId
Message.taskId
```

因此客户端不能等待服务端“返回 tenant”后再区分任务。更稳妥的做法是：客户端在发起请求前就知道自己正在用哪个本地工作区、哪个账号凭据、哪个远端 Agent endpoint，并把这些本地维度和服务端返回的 `taskId/contextId` 一起存入本地映射表。

## 11. 第八步：任务状态生命周期

A2A TaskState：

```text
TASK_STATE_UNSPECIFIED
TASK_STATE_SUBMITTED
TASK_STATE_WORKING
TASK_STATE_COMPLETED
TASK_STATE_FAILED
TASK_STATE_CANCELED
TASK_STATE_INPUT_REQUIRED
TASK_STATE_REJECTED
TASK_STATE_AUTH_REQUIRED
```

实战状态机可以理解为：

```text
SUBMITTED
  -> WORKING
  -> INPUT_REQUIRED -> WORKING
  -> AUTH_REQUIRED  -> WORKING
  -> COMPLETED
  -> FAILED
  -> CANCELED
  -> REJECTED
```

终态不可重启：

```text
COMPLETED / FAILED / CANCELED / REJECTED
```

一旦任务进入终态，客户端不应再向同一个 taskId 发送继续消息。后续修改、追问、 refinement 应创建新 task，但复用相同 `contextId`，并通过 `referenceTaskIds` 引用旧任务。

## 12. 第九步：获取任务进度

### 12.1 Polling

```http
GET /tasks/{taskId}?historyLength=20 HTTP/1.1
A2A-Version: 1.0
Authorization: Bearer <token>
```

客户端循环：

```text
while task not terminal and not interrupted:
  GetTask(taskId)
  update local task view
  if status is INPUT_REQUIRED or AUTH_REQUIRED:
    break and wait user/client action
  sleep/backoff
```

### 12.2 Streaming

使用 `SendStreamingMessage`：

```http
POST /message:stream HTTP/1.1
Content-Type: application/a2a+json
Accept: text/event-stream
A2A-Version: 1.0
```

服务端返回 SSE：

```text
data: {"task": {...}}

data: {"statusUpdate": {...}}

data: {"artifactUpdate": {...}}
```

流式事件要求按生成顺序传输。多个客户端可订阅同一任务，每条 stream 应收到相同顺序的事件。

### 12.3 Artifact chunk 拼接

`TaskArtifactUpdateEvent` 包含：

```text
artifact
append
lastChunk
```

客户端应按 `artifactId` 维护本地缓冲：

```text
if append == false:
  replace artifact buffer
else:
  append parts to existing artifact buffer

if lastChunk == true:
  mark artifact complete
```

## 13. 第十步：暂停任务

A2A 没有一个名为 `PauseTask` 的标准操作。实战中“暂停”主要由服务端通过 Task 状态表达。

### 13.1 需要用户输入的暂停

服务端进入：

```text
TASK_STATE_INPUT_REQUIRED
```

含义：服务端 Agent 需要客户端或用户补充信息才能继续。

服务端应在 `TaskStatus.message` 中说明需要什么输入：

```json
{
  "status": {
    "state": "TASK_STATE_INPUT_REQUIRED",
    "message": {
      "messageId": "msg-agent-need-input-001",
      "role": "ROLE_AGENT",
      "parts": [
        {
          "text": "请提供目标部署环境：本地、测试服务器还是生产服务器？"
        }
      ]
    }
  }
}
```

客户端处理：

```text
1. 停止等待自动完成
2. 将 task 标记为等待输入
3. 展示 status.message 给用户
4. 保存 taskId/contextId
5. 等用户输入后恢复
```

### 13.2 需要认证的暂停

服务端进入：

```text
TASK_STATE_AUTH_REQUIRED
```

含义：服务端需要客户端完成认证或补充权限。

客户端处理：

```text
1. 展示认证需求
2. 执行 OAuth / API Key / OIDC 等认证流程
3. 获取新凭据
4. 用相同 taskId/contextId 发送继续消息
```

### 13.3 客户端主动暂停

A2A v1.0 标准里没有通用的客户端主动暂停操作。客户端如果想“不再持续监听”，可以：

- 关闭 SSE 连接，但这不等于暂停服务端任务。
- 停止 polling，但这不等于暂停服务端任务。
- 如果希望停止任务，应调用 `CancelTask`。
- 如果需要业务级暂停，需要服务端自定义语义或扩展。

因此 AgentRuntime 设计中要区分：

```text
客户端停止观察 != 服务端任务暂停
服务端 INPUT_REQUIRED/AUTH_REQUIRED = 协议层可表达的中断等待
CancelTask = 请求取消任务
业务暂停 = 需要服务端能力或扩展约定
```

## 14. 第十一步：恢复任务

恢复 `INPUT_REQUIRED` 或 `AUTH_REQUIRED` 的任务，本质是继续发送一条 Message。

客户端必须带上：

- 同一个 `taskId`
- 同一个 `contextId`
- 新的 `messageId`
- 用户补充内容或认证完成说明

### 14.1 HTTP+JSON 示例

```http
POST /message:send HTTP/1.1
Content-Type: application/a2a+json
A2A-Version: 1.0
Authorization: Bearer <new-or-existing-token>
```

```json
{
  "message": {
    "messageId": "msg-user-resume-001",
    "contextId": "ctx-abc",
    "taskId": "task-123",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "目标部署环境是测试服务器，允许重启服务。",
        "mediaType": "text/plain"
      }
    ]
  },
  "configuration": {
    "acceptedOutputModes": ["text/plain", "application/json"],
    "returnImmediately": true
  }
}
```

### 14.2 恢复后的服务端行为

服务端收到恢复消息后，可以：

- 将同一 Task 从 `INPUT_REQUIRED` 转回 `WORKING`。
- 继续内部 Agent loop。
- 再次进入 `INPUT_REQUIRED` 或 `AUTH_REQUIRED`。
- 进入终态。

客户端不应假设一次恢复必然完成任务。

## 15. 第十二步：任务执行中继续发新的提示消息

A2A 支持多轮交互。客户端可以在任务未终结时继续发送 Message。

### 15.1 继续同一个任务

如果是补充当前任务的信息，应带上同一个 `taskId` 和 `contextId`：

```json
{
  "message": {
    "messageId": "msg-user-extra-001",
    "contextId": "ctx-abc",
    "taskId": "task-123",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "补充要求：报告中增加风险评估章节。"
      }
    ]
  }
}
```

服务端可以接受，也可以根据自身状态拒绝。规范说 Agent 可以接受 non-terminal task 的 additional messages，但不是所有服务端都必须支持任意时刻插入新提示。

### 15.2 在同一上下文中新建任务

如果是基于已有上下文发起新工作，但不是继续同一任务，可以只带 `contextId`，不带 `taskId`：

```json
{
  "message": {
    "messageId": "msg-user-new-task-001",
    "contextId": "ctx-abc",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "基于刚才的架构报告，再生成一份迁移计划。"
      }
    ],
    "referenceTaskIds": ["task-123"]
  }
}
```

服务端应创建新 Task，并把它归入同一个 `contextId`。

### 15.3 终态任务不能恢复

如果原任务已经：

```text
COMPLETED / FAILED / CANCELED / REJECTED
```

客户端不应向旧 `taskId` 继续发送消息。应创建新任务，并使用：

```text
contextId = 原 contextId
referenceTaskIds = [旧 taskId]
```

### 15.4 已完成任务后继续优化怎么续上

如果一个有状态的 Agent Server 已经完成了任务，客户端想让它“接着刚才的结果继续优化”，协议层不要向旧 `taskId` 继续发送消息。正确方式是创建一个新 Task，并让它继承同一个 `contextId`，同时用 `referenceTaskIds` 明确引用已完成任务。

示例：

```json
{
  "message": {
    "messageId": "msg-user-optimize-001",
    "contextId": "ctx-abc",
    "role": "ROLE_USER",
    "parts": [
      {
        "text": "基于刚才完成的架构报告，继续优化性能分析章节，并补充数据库瓶颈建议。"
      }
    ],
    "referenceTaskIds": ["task-123"]
  },
  "configuration": {
    "acceptedOutputModes": ["text/markdown", "text/plain"],
    "returnImmediately": true
  }
}
```

服务端如果保留了 `ctx-abc` 对应的上下文，就可以把新任务理解为“在刚才上下文中继续优化”。它应返回一个新的 `taskId`：

```text
旧任务：task-123，contextId = ctx-abc，状态 = COMPLETED
新任务：task-456，contextId = ctx-abc，referenceTaskIds = [task-123]
```

这意味着：

```text
续上的是 context，不是旧 task。
优化是一个新 task，不是 completed task 重新变成 working。
```

A2A 提供 `contextId` 和 `referenceTaskIds` 来表达这种连续性，但不强制服务端永久保存上下文。客户端应同时保存最终 artifacts。如果服务端上下文过期、旧任务被清理或无法访问，客户端应把上一次 artifacts 的关键内容重新作为新 Message 的输入，或者提示用户无法继续原上下文。

## 16. 第十三步：服务端 Agent 某次 LLM 返回普通文本但没有函数调用怎么办

这是 AgentRuntime 接入 A2A 时非常重要的边界。

A2A 协议不规定服务端 Agent 内部必须采用“LLM + tool calling loop”。A2A 只观察服务端对外暴露的协议状态：

```text
Message
Task
TaskStatus
Artifact
```

因此，“服务端某次 LLM 返回普通文本没有函数调用”不是 A2A 协议层事件，而是服务端实现层事件。服务端必须把这个内部结果翻译成 A2A 可理解的外部状态。

### 16.1 普通文本是最终回答

如果普通文本已经完成任务，服务端应进入：

```text
TASK_STATE_COMPLETED
```

并把结果作为 Artifact 返回。可以同时在 `status.message` 或 history 中保留说明性 Message，但真正任务结果应优先是 Artifact。

示例：

```json
{
  "task": {
    "id": "task-123",
    "contextId": "ctx-abc",
    "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {
        "messageId": "msg-agent-complete-001",
        "role": "ROLE_AGENT",
        "parts": [
          {
            "text": "架构分析已完成。"
          }
        ]
      }
    },
    "artifacts": [
      {
        "artifactId": "artifact-report-001",
        "name": "architecture-report.md",
        "description": "仓库架构分析报告。",
        "parts": [
          {
            "text": "# 架构报告\n\n...",
            "mediaType": "text/markdown"
          }
        ]
      }
    ]
  }
}
```

### 16.2 普通文本是在向用户提问

如果普通文本表达的是“我需要更多信息”，服务端应进入：

```text
TASK_STATE_INPUT_REQUIRED
```

并把问题放在 `TaskStatus.message`。

客户端看到 `INPUT_REQUIRED` 后暂停等待用户输入，然后用同一个 `taskId/contextId` 恢复。

### 16.3 普通文本是在说明需要认证

如果普通文本表达的是“需要登录 / 授权 / 权限不足”，服务端应进入：

```text
TASK_STATE_AUTH_REQUIRED
```

客户端完成认证后继续发送 Message。

### 16.4 普通文本只是中间进度

如果普通文本只是阶段性说明，例如“我正在分析依赖关系”，服务端可以：

- 继续保持 `TASK_STATE_WORKING`。
- 通过 `TaskStatusUpdateEvent.status.message` 发送进度文本。
- 不把它当最终结果。

### 16.5 普通文本无法判断任务是否完成

如果服务端内部 Agent loop 依赖工具调用来决定是否继续，而 LLM 返回了普通文本但没有工具调用，服务端不能让任务状态悬空。

服务端实现必须做一个明确决策：

```text
1. 把普通文本判定为最终答复 -> COMPLETED + Artifact
2. 把普通文本判定为询问用户 -> INPUT_REQUIRED
3. 把普通文本判定为需要认证 -> AUTH_REQUIRED
4. 把普通文本判定为失败/无效响应 -> FAILED
5. 把普通文本当作中间状态并继续内部 loop -> WORKING
```

对 AgentRuntime 来说，不能简单认为“没有函数调用就暂停”。更合理的是：

```text
LLM 普通文本 + 明确完成语义 -> end task / completed
LLM 普通文本 + 明确提问语义 -> input required / pause
LLM 普通文本 + 权限语义 -> auth required
LLM 普通文本 + 无法推进且不符合协议预期 -> failed 或实现层重试
LLM 普通文本 + 只是进度说明 -> working 并继续服务端 loop
```

也就是说，是否暂停不是由“有没有函数调用”直接决定，而是由服务端 Agent 对该 LLM 输出的业务语义判断决定。

### 16.6 服务端 Agent loop 与 A2A 状态的映射建议

如果服务端是 AgentRuntime，内部可能有本地工具循环控制：

```text
continue
pause
complete
terminate
fail
```

可映射为：

```text
continue  -> TASK_STATE_WORKING
pause     -> TASK_STATE_INPUT_REQUIRED
complete  -> TASK_STATE_COMPLETED
terminate -> TASK_STATE_CANCELED 或 TASK_STATE_REJECTED，取决于原因
fail      -> TASK_STATE_FAILED
```

普通 LLM 文本没有工具调用时，Runtime 应通过输出分类或生命周期策略决定映射结果，而不是默认永久等待下一次 tool call。

## 17. 第十四步：取消任务

客户端可以调用：

```text
CancelTask(taskId)
```

HTTP+JSON：

```http
POST /tasks/task-123:cancel HTTP/1.1
A2A-Version: 1.0
Authorization: Bearer <token>
```

语义：

- 如果任务可取消，服务端返回状态更新后的 Task。
- 如果任务已终态或不可取消，返回 `TaskNotCancelableError`。
- CancelTask 是幂等操作，多次取消应具有相同效果。

客户端不能把“断开 SSE”当作取消。断开 SSE 只是停止接收事件，任务可能仍在服务端继续运行。

## 18. 第十五步：任务结束与结果返回

任务结束时，状态应是终态之一：

```text
TASK_STATE_COMPLETED
TASK_STATE_FAILED
TASK_STATE_CANCELED
TASK_STATE_REJECTED
```

### 18.1 Completed

成功完成时，客户端读取：

- `task.artifacts`
- `task.status.message`
- `task.history`
- `task.metadata`

任务结果优先从 `artifacts` 获取。

### 18.2 Failed

失败时，客户端读取：

- `task.status.message`
- 错误 metadata
- history 中的重要消息

客户端可以展示失败原因，并允许用户用相同 `contextId` 创建新任务重试。

### 18.3 Canceled

取消时，客户端应：

- 停止 polling / streaming。
- 标记本地任务取消。
- 读取可能已有的部分 artifacts。
- 不再向该 taskId 发送继续消息。

### 18.4 Rejected

拒绝通常表示服务端 Agent 决定不执行该任务，例如能力不匹配、策略拒绝、安全拒绝。客户端应展示拒绝原因，并根据需要选择其他 Agent。

## 19. 第十六步：收尾处理

客户端在任务结束或中断后应执行收尾。

### 19.1 终态收尾

```text
1. 停止 polling 定时器
2. 关闭 SSE 连接
3. 删除或停用 push notification config，如果不再需要
4. 拉取最终 GetTask，确保拿到完整 artifacts
5. 校验 artifact chunk 是否完整
6. 保存 taskId/contextId/artifactId 映射
7. 将结果写入本地会话、资源库或 UI
8. 标记任务为 completed/failed/canceled/rejected
```

### 19.2 中断态收尾

如果任务进入：

```text
INPUT_REQUIRED / AUTH_REQUIRED
```

客户端不应当作结束，而应保存恢复所需上下文：

```text
taskId
contextId
status.message
required auth/input details
current artifacts
historyLength checkpoint
```

之后等待用户输入或认证完成，再发 `SendMessage` 恢复。

### 19.3 本地资源收尾

如果 Artifact 包含大文件、URL 或二进制：

- 保存可重复读取的 URL、文件路径、artifactId、taskId。
- 不要只保存内部临时 resource id。
- 对 raw/base64 大内容应落盘或进入资源系统。
- 对 URL artifact 应记录原始 URL 和访问权限。

## 20. 客户端实战伪代码

```python
agent_card = fetch_agent_card(agent_card_url)
validate_version(agent_card, required_version="1.0")
interface = select_interface(agent_card.supported_interfaces, preferred="HTTP+JSON")
auth = resolve_auth(agent_card.security_schemes, agent_card.security_requirements)

if wants_streaming and agent_card.capabilities.streaming:
    stream = send_streaming_message(
        interface.url,
        message={
            "messageId": new_message_id(),
            "role": "ROLE_USER",
            "parts": [{"text": user_prompt, "mediaType": "text/plain"}],
        },
        headers={"A2A-Version": "1.0", **auth.headers},
    )

    for event in stream:
        if event.task:
            task = event.task
            remember(task.id, task.contextId)
        elif event.statusUpdate:
            update_status(event.statusUpdate)
            if event.statusUpdate.status.state in ["TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"]:
                close_stream()
                wait_for_user_or_auth()
                break
            if event.statusUpdate.status.state in terminal_states:
                close_stream()
                break
        elif event.artifactUpdate:
            merge_artifact_chunk(event.artifactUpdate)

else:
    response = send_message(
        interface.url,
        message={
            "messageId": new_message_id(),
            "role": "ROLE_USER",
            "parts": [{"text": user_prompt, "mediaType": "text/plain"}],
        },
        configuration={"returnImmediately": True},
        headers={"A2A-Version": "1.0", **auth.headers},
    )

    if response.message:
        show_message(response.message)
    else:
        task = response.task
        while task.status.state not in terminal_states:
            if task.status.state in interrupted_states:
                wait_for_user_or_auth()
                break
            sleep(backoff())
            task = get_task(task.id, history_length=20)

if user_input_for_existing_task:
    send_message(
        interface.url,
        message={
            "messageId": new_message_id(),
            "contextId": task.contextId,
            "taskId": task.id,
            "role": "ROLE_USER",
            "parts": [{"text": user_input, "mediaType": "text/plain"}],
        },
        headers={"A2A-Version": "1.0", **auth.headers},
    )

if task.status.state == "TASK_STATE_COMPLETED":
    final_task = get_task(task.id, history_length=50)
    persist_artifacts(final_task.artifacts)
```

## 21. AgentRuntime 接入 A2A 的实现要点

### 21.1 作为 A2A Client

AgentRuntime 需要：

- Remote AgentCard 配置与缓存。
- AgentCard 签名与版本校验。
- supportedInterfaces 选择器。
- A2A HTTP+JSON / JSON-RPC client。
- A2A 认证管理。
- SendMessage / SendStreamingMessage / GetTask / SubscribeToTask / CancelTask 能力。
- Artifact chunk 合并与资源落地。
- 将远端 AgentCard.skills 映射为本地可调度能力。

### 21.2 作为 A2A Server

AgentRuntime 需要：

- 暴露 `.well-known/agent-card.json`。
- 暴露 `/message:send`、`/message:stream`、`/tasks/{id}` 等接口。
- 把入站 A2A Message 映射到内部 Conversation。
- 把内部 Agent 执行映射到 A2A Task。
- 把内部工具/LLM/生命周期事件映射到 TaskStatusUpdateEvent。
- 把文件、报告、结构化输出映射到 Artifact。

### 21.3 不要把 A2A Message 等同于本地聊天消息

A2A Message 是协议通信单元；本地 Chat Bubble 是 UI 表达；Conversation Message 是运行时上下文。三者不能混为一个模型。

建议映射关系：

```text
A2A Message
  -> Runtime Conversation Message
  -> 可选 UI Chat Bubble

A2A Artifact
  -> Runtime Resource / Attachment / Generated Output
  -> UI Result Card

A2A TaskStatusUpdateEvent
  -> Runtime Process Event
  -> UI Process Bubble / Progress Row
```

### 21.4 服务端普通文本无工具调用的策略

AgentRuntime 如果作为 A2A Server，必须显式定义内部 Agent 输出到 A2A 状态的映射策略。不能让任务因为“LLM 没有 tool call”而无限保持 WORKING。

推荐策略：

```text
1. 如果命中生命周期工具 complete -> COMPLETED
2. 如果命中生命周期工具 pause/message_user -> INPUT_REQUIRED
3. 如果命中生命周期工具 fail -> FAILED
4. 如果 LLM 普通文本符合最终答案 -> COMPLETED + Artifact
5. 如果 LLM 普通文本是问题 -> INPUT_REQUIRED
6. 如果 LLM 普通文本是权限问题 -> AUTH_REQUIRED
7. 如果无法判断且违反服务端执行协议 -> FAILED 或内部重试
```

## 22. 常见误区

### 22.1 A2A 有中心注册中心

A2A 不强制中心化注册中心。AgentCard 可以来自 well-known、直接配置、私有 registry 或企业 catalog。

### 22.2 本地 Agent 必须公网暴露

本文档中本地 Agent 只按 localhost/private A2A Server 处理，仅供本机 AgentRuntime、IDE 或本机 orchestrator 调用，不设计远端可调用路径。

### 22.3 断开流就是暂停任务

不是。断开 SSE 只是客户端不再接收事件。服务端任务是否暂停由 TaskState 表达。

### 22.4 终态任务可以恢复

不能。终态任务不可重启。后续 refinement 应创建新任务，复用 contextId，并通过 referenceTaskIds 引用旧任务。

### 22.5 Message 就是任务结果

不是。Message 用于沟通、状态、澄清；任务结果应优先使用 Artifact。

## 23. 推荐的第一阶段落地范围

AgentRuntime 第一阶段 A2A Client 可先实现：

```text
1. 静态配置 remote AgentCard URL
2. 拉取并缓存 AgentCard
3. HTTP+JSON SendMessage
4. HTTP+JSON GetTask
5. SendStreamingMessage SSE
6. TaskStatusUpdateEvent 处理
7. TaskArtifactUpdateEvent chunk 合并
8. INPUT_REQUIRED 恢复流程
9. COMPLETED Artifact 落地
```

AgentRuntime 第一阶段 A2A Server 可先实现：

```text
1. localhost/private AgentCard
2. /message:send
3. /message:stream
4. /tasks/{id}
5. 内部 Agent task 到 A2A Task 映射
6. 内部 process event 到 A2A status/artifact event 映射
7. 普通文本无工具调用时的明确状态决策
```

暂缓：

```text
1. Push notification
2. AgentCard JWS 签名
3. 私有 registry/catalog
4. gRPC binding
5. 多租户路径
```

不纳入范围：

```text
1. 本地 Agent 的公网暴露
2. tunnel / gateway / relay 远端暴露方案
```

## 24. 一句话总结

从客户端实战角度看，A2A 的核心不是“发一条聊天消息给远端 Agent”，而是：

```text
发现 AgentCard
  -> 选择协议和认证
  -> 发送 Message
  -> 管理 Task 生命周期
  -> 在 interrupted state 下恢复
  -> 在 terminal state 后通过新 Task 做 refinement
  -> 用 Artifact 接收真正任务结果
```

AgentRuntime 接入 A2A 时，必须把远端 Agent 调用建模为有状态任务协作，而不是一次普通 HTTP 调用或一次 LLM 聊天补全。
