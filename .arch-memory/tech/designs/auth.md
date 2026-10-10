# auth · 技术设计

## 会话续期
- 方案：续期沿用原 token，在认证成功时与 last_seen_at 一并写入，不提供刷新接口
- 不变量：续期仅限 runtime_auth_middleware 与 /api/auth/me，bootstrap guard 与通信流复核只读校验
- 禁止：不得新增会话表字段记录绝对上限，上限由 created_at 推导
