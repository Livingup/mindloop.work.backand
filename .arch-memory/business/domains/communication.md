# communication

## 边界
- 不负责登录会话判定（→ auth）。

## 需求约束
- COMMUNICATION-01 通信流 SSE 连接所属的登录会话过期或被撤销后，必须在 15 秒内推送 communication.auth_invalid 事件并关闭连接。
