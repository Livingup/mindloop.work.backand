# 业务总览

## 领域清单
| 域 | 类型 | 职责 | 文件 |
|---|---|---|---|
| auth | 支撑 | 访问本机 Runtime 的本地账号、登录会话与认证判定 | domains/auth.md |
| communication | 支撑 | 业务无关的逻辑通信流、消息持久化与 SSE 推送 | domains/communication.md |

## 域间关系
- communication → auth：同步，通信流 SSE 按 COMMUNICATION-01 复核登录会话
