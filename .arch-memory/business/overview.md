# 业务总览

## 领域清单
| 域 | 类型 | 职责 | 文件 |
|---|---|---|---|
| auth | 支撑 | 访问本机 Runtime 的本地账号、登录会话与认证判定 | domains/auth.md |
| communication | 支撑 | 业务无关的逻辑通信流、消息持久化与 SSE 推送 | domains/communication.md |
| task_note | 支撑 | 后台观察提炼业务 Agent 的工作记忆，并以 Note 快照替换旧上下文前缀 | domains/task_note.md |

## 域间关系
- communication → auth：同步，通信流 SSE 按 COMMUNICATION-01 复核登录会话
- task_note → communication：事件，观察者 Note 工具成功后向目标 Agent 发布 task_note.updated；替换完成后发布 agent.task_note.context_swapped
- task_note → conversation_message（未登记为域的模块）：同步，tracking 只经 get/cas_task_note_tracking 读写
- agent 执行引擎、subagent、lacp、file_tools、terminal（未登记为域的模块）→ task_note：同步，调用触发观察与替换接口、初始化子会话 tracking、写入事实记录
- tool 分发器、lacp（未登记为域的模块）→ communication：事件，业务工具或 Note 工具成功后、lacp 入站时按 task_note 口径发布 task_note.updated
