# communication · 技术设计

## 通信流会话复核
- 方案：communication 不依赖 auth，复核函数由 server.create_app 注入 create_communication_router
- 禁止：communication.auth_invalid 帧不得带 id，不得推进业务 seq
