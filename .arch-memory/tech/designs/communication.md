# communication · 技术设计

## 通信流会话复核
- 方案：COMMUNICATION-01 的会话复核由 Runtime 组合根注入校验能力，communication 不依赖 auth
- 禁止：communication.auth_invalid 事件帧不得携带 id，不得推进业务 seq
