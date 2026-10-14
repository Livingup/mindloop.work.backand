# -*- coding: utf-8 -*-
"""Scene 模块 — 项目（Project）与场景（Scene）领域管理。

业务概念边界：
- Project：一个项目目录（≈ 一个 git workspace 根），长期存在，用户开/关项目。
- Scene：项目内的一个长期任务上下文（如"实现登录页"、"重构鉴权"），跨多个 AgentTask。
- AgentTask：scene 内的一次 Agent 执行单元（既有概念）；同 scene 内多个 task 串行/并行。

stream 归属：communication stream 以 scene 为单位（stream-scene-{scene_id}），整个 scene
共享一个流，前端只要订阅 scene_id 就能持续接收同 scene 内所有 task 的事件，刷新页面/重连
只需带 after_seq 即可恢复完整历史。

架构分层：
- project.py / scene.py：领域聚合根（frozen dataclass + 业务规则校验）
- scene_repository.py：仓储接口（Protocol，领域层）
- infrastructure/sqlite_scene_repository.py：SQLite 仓储实现（基础设施层）
- scene_service.py：领域服务（编排仓储完成业务用例）
- north_gateway/scene_contracts.py：北向网关契约（Gateway Protocol + DTO + Request 模型）
- north_gateway/scene_gateway_impl.py：网关实现
- north_gateway/scene_rest_adapter.py：REST 接入适配
- scene_factory.py：依赖装配工厂（组合根）
- scene_config.py：模块配置
"""
