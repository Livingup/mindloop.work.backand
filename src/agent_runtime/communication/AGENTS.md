# 通用通信模块（communication）

负责业务无关的逻辑通信流、通信消息、消息日志持久化和后端主动推送接入能力。

## 模块边界

本模块只表达通用通信抽象，不承载 Agent、LLM、工具、终端等具体业务规则。

- `CommunicationStream` 是逻辑消息流，不绑定 SSE、WebSocket 或具体前端连接。
- `CommunicationMessage` 是统一消息信封，业务语义由 `type`、`topic`、`payload` 和 `metadata` 表达。
- `CommunicationRepository` 负责通信流和消息历史持久化。
- `InMemoryCommunicationMessageBus` 负责当前进程内实时订阅分发。
- REST/SSE adapter 只负责 HTTP 接入，业务层和运行层不直接依赖物理推送通道。

## 当前能力

- 创建、查询和关闭逻辑通信流。
- 发布通用通信消息并写入 SQLite 消息日志。
- 按 `stream_id`、`after_seq` 增量读取历史消息。
- 通过 `/api/communication/streams/{stream_id}/events` 使用 SSE 订阅实时消息。
- 提供 `RuntimeCommunicationContext` 和 `RuntimeEventPublisher`，供 Agent 执行链路透传逻辑通信流并发布运行时事件。
