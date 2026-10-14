# 技术总览

## 架构基线
- 分层：按模块分包 agent_runtime/<模块>/，模块内为领域服务、models、infrastructure 仓储、north_gateway REST 适配器、factory 组合根，无独立 app 层
- 命名：*Service、Sqlite*Repository、create_*_router、*Factory，REST 入参 *Body、出参 *Response
- 逻辑承载：业务规则写在领域服务，REST 适配器只做 HTTP 映射与限流
- 跨模块与横切：server.create_app 为全局组合根，认证、bootstrap、网络监控以 HTTP middleware 写在 server.py
- 持久化：sqlite3 手写 SQL，每次操作独立连接并提交，表结构在连接时以 CREATE TABLE IF NOT EXISTS 保证，行转 frozen dataclass
- 持久化（task_note）：Note 数据用 PostgreSQL（psycopg，复用 cloud_user_config_database），会话 tracking 用 SQLite JSON 加版本列 CAS
- 横切（task_note）：观察会话由后台 asyncio 任务承载，目标身份经 contextvars 注入；task_note 服务为模块级单例（get_/set_），由 agent_execution_factory 装配；单个后端进程部署，实例锁与观察完成表在进程内存，多个业务 Agent 在进程内并发
- 错误：领域服务抛 ValueError，由适配器或中间件转为 HTTP 响应

## 全局技术约定
- TECH-01 认证中间件判定失败的 401 请求诊断日志（network-backend.jsonl 与 stderr）每个“方法 + 路径”每 60 秒仅限 1 条完整记录，其余只计入 network.snapshot 的 suppressed_auth_failure_records 累计数。
