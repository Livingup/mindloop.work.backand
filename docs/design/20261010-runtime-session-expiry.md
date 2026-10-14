# 20261010-runtime-session-expiry

## 0. 元信息
- 仓库：agentruntime master @ 2cc2e8f5aae4ff52f43271eb116d1cd32914bbe5
- 仓库：agent-frontend master @ 038ce3d798673f86c6a1ac1c3bfbe65552db6b94
- 设计阶段：事实观察 → 后端设计 → 前端设计；跳过 UI 设计（过期提示复用现有登录卡片 errorMessage 区域，无新增页面/交互）
- 设计档位：后端 标准设计；前端 标准设计
- 用户确认记录：
  - 裁剪确认单：后端 → 前端，跳过 UI。用户原话：“确认：后端 → 前端，跳过 UI”
  - 后端架构基线 + 决策 D1/D2/D3：用户原话：“全部按建议，D3 把省略计数放进 snapshot”
  - 后端完整方案终审：用户原话：“通过，按此定稿（含规范性条目）”
  - 前端完整方案终审：用户原话：“通过，按此定稿”
  - 组装文档最终审核及记忆写入、提交授权：用户原话：“文档通过，写入记忆并提交”
- 交付范围：设计文档与架构记忆；设计中的执行步骤和测试是交给执行侧的清单，本任务未执行。
- 组装依据：后端、前端各自已经用户确认的原定稿；编排曾发出的额外修订要求已撤回，两位设计师确认未改动原定稿。
- 基线补充核对：后端当前分支期间前进至 d050d6bf58cc36999c643122dd9ca6d5e4a0834a；相对设计基线仅修改 terminal_service.py 与 test_terminal_capacity_phase1.py。事实观察确认本次 auth、communication、server.py、网络监控及 AGENTS 规范未变化。前端 HEAD 未变化。
- 章节组装：仅调整标题层级和摆放；后端原“第一节”业务架构位于本文第 3 节，后端原“第三节”技术架构位于 5.2，后端原“第六节”记忆影响位于第 9 节后端分组。

### 后端设计师用户确认记录（原文）
| 阶段 | 确认内容 | 用户原话 |
|---|---|---|
| 架构基线 + 决策 D1/D2/D3 | 基线 6 项照源码归纳的结果确认；D1=A（空闲 30 天滑动续期，绝对上限 90 天）；D2=A（通信流 SSE 每 15 秒复核会话，失效时推 auth_invalid 并关流）；D3=A（认证失败日志按 60 秒窗口采样），省略的条数放进 network.snapshot | “全部按建议，D3 把省略计数放进 snapshot” |
| 完整方案终审 | 全文定稿，包含规范性条目（架构基线、AUTH-01~AUTH-04、COMMUNICATION-01、TECH-01） | “通过，按此定稿（含规范性条目）” |

确认过程中用户问过 network.snapshot 的用途。我答复的要点：仓库里没有设计说明，该快照来自提交 df70fc8（2026-05-25，Add backend network monitoring），每 10 秒写一条网络层计数，用于事后排查进程存活、连接或订阅泄漏、背压和日志丢失。

### 前端设计师用户确认记录（原文）
| 阶段 | 确认内容 | 用户原话 |
|---|---|---|
| 完整方案终审 | 全文定稿，含架构基线、RUNTIME-AUTH-01/RUNTIME-AUTH-02 和记忆条目。两个待拍板点用户没有另外表态，按“通过”取我的建议：会话失效时不清 chat store，与现有 logout 一致；logout 一并清 query 缓存 | “通过，按此定稿” |

### 角色 → 实例 ID
| 角色 | 实例 ID |
|---|---|
| 系统设计编排 | agent-instance-aac7f4abbdab4e06967183201d9b40a9 |
| 事实观察 | agent-instance-d71794d6e4ee49178d34f8b0459aaae7 |
| 后端设计 | agent-instance-276507e7ba684c2a89a3e4421265e746 |
| 前端设计 | agent-instance-c823eabdd317463e9956b1ccc783f373 |
| UI 设计 | 未创建，用户确认跳过 |

## 1. 需求原文

```text
前后端通信异常问题报告（临时事实陈述）

本报告基于当前运行中的系统进程、网络日志、运行错误日志、认证数据库及前后端相关源代码的现场核查整理而成，仅做事实陈述。

一、 日志与现场观察
1. 进程与服务状态
后端进程：
路径：C:\Program Files\Spread AI\resources\backend\spread-ai-runtime\spread-ai-runtime.exe
PID：85996
监听状态：TCP 127.0.0.1:8000 正常 LISTEN。
事件循环状态：event_loop_status=ok，Tick 延迟在 5ms ~ 26ms 之间，无死锁或事件循环阻塞。
数据库工作线程：PostgresCloudUserConfigStore 周期性轮询与缓存刷新正常执行（执行耗时 84ms ~ 220ms，final_state=success）。
前端进程：
路径：C:\Program Files\Spread AI\Spread AI.exe
PID 包含：6492, 9016, 112392, 115612。
2. 日志文件分布与量级
日志目录：C:\Users\李运\AppData\Roaming\spread-ai-web\logs\
观测到的日志文件：
network-backend.jsonl：大小约 8.7 MB，持续写入中。
backend-stderr.log：大小约 1.8 MB，持续高频写入中。
backend-stderr.log.1 至 .5：单文件大小均为 5,242,437 字节（达到 5 MB 轮转阈值），平均每 11 分钟轮转产生一个新文件（18:21, 18:33, 18:44, 18:56, 19:07）。
network-electron.jsonl：大小约 4.8 MB，持续写入中。
3. 网络与请求日志内容
后端网络日志（network-backend.jsonl）： 大量业务与监控接口请求均返回 HTTP 状态码 401：
GET /api/lacp/status -> 401
GET /api/lacp/instances/agent-instance-c0681ecf1fc64d6faabb234356385dc6/registration -> 401
POST /api/agent-runtime/scenes/scene-c3dd9d3671714494bce0b2f4/heartbeat -> 401
POST /api/workspaces/git-status -> 401
后端返回内容体统一为：{"detail": "未登录或登录已过期，请重新登录。"}。
前端主进程/渲染进程日志（network-electron.jsonl）： 所有请求被代理转发并返回 401，渲染进程记录为： {"trace_id":"...","type":"rest.http_error","status":401,"error_type":"http_error","error_message":"未登录或登录已过期，请重新登录。","pid":115612}。
请求频率： 上述失败请求通过定时器每 1 ~ 2 秒发起一轮，且持续处于重试状态。
4. 认证数据库记录（runtime_auth.sqlite3）
数据库路径：C:\Users\李运\.spread-ai\agent-runtime\runtime_auth.sqlite3
runtime_device_sessions 表中当前正在使用的桌面会话记录：
session_id：sess-cd9022fb-a969-40e6-a377-87bd554e15d8
account_id：acct-ba4fda49-f004-40e9-8e85-828ad457db3f
device_id：device-c818bb6a-08f5-4c7f-a0ea-ed632a31b916
client_kind：electron-desktop
created_at：2026-09-09T15:03:24.117015+00:00
last_seen_at：2026-10-09T15:03:22.720232+00:00
expires_at：2026-10-09T15:03:24.117015+00:00
revoked_at：None
事实对比：该会话最后一次被成功处理的时间记录为 2026-10-09 15:03:22 UTC，其设定的过期时间为 2026-10-09 15:03:24 UTC。自该时刻之后的所有请求均被拒绝。
二、 源码观察
1. 后端中间件与认证服务逻辑
鉴权中间件（agent_runtime/server.py）：
runtime_auth_middleware 针对除白名单（如 /api/health、/api/auth/、/api/runtime-bootstrap/）外的所有 /api/ 请求执行鉴权：
python
auth_session = runtime_auth_service.authenticate_token(_runtime_auth_token_from_request(request))
if auth_session is None:
    return JSONResponse(
        {"detail": "未登录或登录已过期，请重新登录。"},
        status_code=401,
    )
会话有效期与验证逻辑（agent_runtime/auth/auth_service.py）：
会话默认寿命设定为 30 天：DEFAULT_SESSION_TTL_DAYS = 30。
创建会话时设定固定过期时间：expires_at = (now + timedelta(days=DEFAULT_SESSION_TTL_DAYS)).isoformat()。
验证逻辑显式校验时间戳：
python
if _parse_datetime(session.expires_at) <= _now():
    return None
活跃更新行为（agent_runtime/auth/infrastructure/sqlite_auth_repository.py）：
每次请求验证通过后调用 touch_session：
sql
UPDATE runtime_device_sessions SET last_seen_at = ? WHERE session_id = ?
该方法仅更新 last_seen_at 字段，不更新 expires_at 字段。
2. 前端客户端与生命周期逻辑
前端认证守卫（RuntimeAuthGate.tsx）：
refreshRuntimeAuth 仅在组件初次挂载（useEffect）时调用一次：
typescript
useEffect(() => {
  void refreshRuntimeAuth()
}, [refreshRuntimeAuth, runtimeBaseVersion])
当状态进入 mode = "ready" 并渲染业务组件后，无定时检查或 401 触发的被动状态更新逻辑。
HTTP 客户端（httpClient.ts）：
当接收到非 2xx 响应（!response.ok）时：
typescript
const message = errorMessageFromBody(body) ?? (response.statusText || "HTTP request failed")
recordRendererNetworkEvent(...)
throw new HttpError(message, response.status, body)
未包含对 401 状态码的全局事件派发、Token 清除或重定向处理。
业务组件通信健康状态处理（ChatPage.tsx）：
在场景连接恢复与心跳机制中：
typescript
const fatalRestoreError = /(?:\b(?:401|403|404|410|426)\b|not found|not_found|unauthorized|forbidden|permission|权限|不存在|已关闭|protocol|协议)/i.test(errorMessage)
当收到 401 报错时，fatalRestoreError 判定为真，将通信健康状态置为： sseStatus: "error"。
UI 呈现“前后端通信连接失败/通信异常”，但页面组件不卸载，内部的定时轮询计时器（LACP、Git status、心跳等）继续运行。
三、 问题梳理
时序节点：
2026-09-09 15:03:24 UTC：客户端登录，后端生成会话 Token，固定过期时间设定为 2026-10-09 15:03:24 UTC。
2026-09-09 至 2026-10-09 15:03:22 UTC：客户端长期开机运行，期间发送的所有请求更新了 last_seen_at，但未改变 expires_at。
2026-10-09 15:03:24 UTC：会话达到 30 天有效期，后端 _parse_datetime(session.expires_at) <= _now() 判定成立，Token 变为过期状态。
拒绝链条：
自 15:03:24 UTC 起，所有需鉴权的业务请求经由 runtime_auth_middleware 校验失败，全量返回 HTTP 401。
前端接收到 401 响应体 {"detail": "未登录或登录已过期，请重新登录。"}。
状态表现：
前端业务层因心跳及恢复请求返回 401，被正则命中为致命恢复故障，将前端通信通道标记为 error，界面展示通信故障。
前端认证根容器 RuntimeAuthGate 未感知到请求级别的 401 错误，组件未回到登录态（mode = "login"）。
前端各后台轮询任务持续以 1~2 秒/次的频率高频重试，后端与前端代理对每一次 401 请求记录日志，引发日志以每 10 分钟 5MB 的速度写入并快速轮转。
看下这个问题
```

用户补充问题原文：
```text
后端给前端提供的api接口，ws连接都有前置token校验吗？
```

需求原文保留用户现场报告的原始表述，不将其全部视为当前源码已核实结论。设计依据为对应设计师定稿；本任务未修改正式安装版、认证数据库或运行服务。

## 3. 业务架构

- **域**：修改 auth（Runtime 本地认证）、修改 communication（通用通信）；不新增域。
- **边界**：auth 负责登录会话的有效性判定和续期，不管云账号和远程访问通道。communication 自己不判定会话，只执行组合根注入的校验函数。
- **关键概念**：登录会话，指一次登录或首次初始化创建的设备会话，凭 Bearer token 访问 Runtime。续期时 token 不变。
- **状态流转**：
  - 有效 →(认证成功) 有效，过期时间顺延
  - 有效 →(空闲超过 30 天，或到达创建后 90 天) 已过期
  - 有效 →(登出) 已撤销
  - 已过期、已撤销都是终态
- **不变量**：
  - 过期时间 = min(最近一次认证成功时刻 + 30 天, 创建时刻 + 90 天)
  - 只要库里存的 expires_at ≤ 当前时间，会话就是已过期，续期不能把它救回来
- **跨域交互**：communication → auth，同步调用。通信流 SSE 每 15 秒调用一次组合根注入的只读校验函数。
- **需求约束**：
  - AUTH-01 Runtime 登录会话每次认证成功后，过期时间必须顺延至当前时间后 30 天。
  - AUTH-02 Runtime 登录会话的过期时间不得晚于会话创建时间后 90 天。
  - AUTH-03 已过期或已撤销的 Runtime 登录会话不得因后续请求恢复有效。
  - AUTH-04 认证失败的 401 响应必须携带错误码，区分令牌缺失、令牌无效、会话过期、会话撤销、账号未初始化。
  - COMMUNICATION-01 通信流 SSE 连接所属的登录会话过期或被撤销后，必须在 15 秒内推送 communication.auth_invalid 事件并关闭连接。

## 4. 接口契约

### 4.1 认证失败 401（所有受保护的 /api/*，包括 SSE 握手）
状态码 401，Content-Type: application/json

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| detail | string | 否 | 已有账号时固定为“未登录或登录已过期，请重新登录。”；无账号时保持原文案 |
| code | string 枚举 | 否 | 见下表 |
| bootstrap_required / auth_required | boolean | 仅无账号时出现 | 和现状一样 |

| code | 触发条件（按判定顺序） |
|---|---|
| auth_bootstrap_required | Runtime 还没有任何账号，且路径不在无账号白名单内 |
| auth_token_missing | 既没有 Authorization: Bearer，也没有允许使用的 query token |
| auth_token_invalid | token 对应不到会话，或会话的账号不存在或非 active |
| auth_session_revoked | 会话已登出（revoked_at 非空） |
| auth_session_expired | expires_at ≤ 当前 UTC 时间 |

前端判定规则：401 且 body 里有 code、code 以 auth_ 开头，就是 Runtime 会话失效。POST /api/auth/login 密码错误时的 401 不带 code，body 仍是 {"detail":"账号或密码错误。"}，前端不得把它当成会话失效。

### 4.2 GET /api/auth/me（RuntimeAuthMeResponse）

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| authenticated | boolean | 否 | 和现状一样 |
| account | RuntimeAuthAccountResponse | 是 | 未认证时为 null |
| session | RuntimeAuthDeviceSessionResponse | 是 | 认证成功时，expires_at 和 last_seen_at 都是本次续期之后的值（ISO 8601，UTC） |
| code（新） | string 枚举 | 是 | 认证成功时为 null；未认证时取 auth_token_missing / auth_token_invalid / auth_session_revoked / auth_session_expired 之一 |

状态码仍为 200。调用 /me 本身算一次认证成功，会续期。

### 4.3 login / bootstrap / logout / status
结构不变。login 和 bootstrap 返回的 session.expires_at = 创建时间 + 30 天。续期不换 token，不新增刷新接口。

### 4.4 SSE GET /api/communication/streams/{stream_id}/events 新增事件
```
event: communication.auth_invalid
data: {"stream_id":"<string>","code":"auth_session_expired","detail":"未登录或登录已过期，请重新登录。","server_time":"<ISO 8601 UTC>"}
```
- 不带 id: 行，不推进业务 seq，也不写入通信消息日志。
- code 只取 auth_token_missing / auth_token_invalid / auth_session_revoked / auth_session_expired 之一。
- 服务端发完这一帧就结束响应。前端收到后必须调用 EventSource.close()，否则浏览器会自动重连，又拿到握手 401。
- 复核间隔：距上次复核满 15 秒才复核。首次校验由连接建立时的中间件完成。所以从会话失效到流关闭，最多是 15 秒加一个等待周期。
- 重新登录后，前端按 after_seq 或 Last-Event-ID 续传，补回缺失的消息。

以上都不是列表接口，不涉及分页、长整型 ID 和幂等键。

### 4.5 给前端设计的要点（契约摘要，后端设计师原文）
1. 会话失效的判定：HTTP 401，且 body.code 以 `auth_` 开头。login 接口密码错误的 401 不带 code，要排除。
2. /api/auth/me 返回 authenticated=false 时会带 code。认证成功时 session.expires_at 是续期后的值。
3. 通信流 SSE 会收到 `communication.auth_invalid`（不带 id），收到后必须调用 EventSource.close()。重新登录后用 after_seq / Last-Event-ID 续传。
4. 上传进度 SSE 和文件 content 不做服务端复核，失效只体现在握手 401 上。
5. 后端的日志采样只是兜底，401 之后停止轮询和重连、回到登录态，需要前端来做。

## 5. 后端技术架构（含代码架构）与执行清单

### 5.0 执行方案
- **任务**：Runtime 本机登录会话改为滑动续期 + 绝对上限；认证失败的 401 带可识别的错误码；已建立的通信流 SSE 在会话失效后主动关闭；认证失败日志采样。
- **设计档位**：标准设计。判据：修改了对外接口（401 响应体、/api/auth/me、SSE 事件），涉及认证这条安全边界和日志容量。不改表结构，没有重型方案，所以没有升到重设计。
- **项目形态**：老项目，首次接手（没有 .arch-memory/）。架构基线已由用户确认。
- **架构基线**：按模块分包。模块内有领域服务 `*_service.py`、领域模型 `models.py`、SQLite 仓储 `infrastructure/`、REST 适配器 `north_gateway/`。横切中间件和组合根放在 `server.py`。领域服务抛 ValueError，由适配器或中间件转成 HTTP 响应。依据：auth/、communication/、server.py。
- **skill 使用**：backend-architecture-standard 用于判定归属，载体服从基线；tech-architecture-standard 用于第三节逐项结论；arch-memory-update 用于第六节。
- **本次不做**：refresh token 和 token 轮换；会话表结构变更；过期会话清理任务；上传进度 SSE 的复核（短任务，到终态即结束）；无账号时 403 local_bootstrap_required 的 body；登录密码错误的 401 body（保持现状，不带 code）；uvicorn access log；Electron network-electron.jsonl 的写入策略；所有前端改动（由前端设计负责）。

### 5.1 代码架构
| 层 | 文件 | 构件 | 职责 |
|---|---|---|---|
| 领域模型 | auth/models.py | RuntimeAuthFailureReason（新） | 认证失败原因的枚举，枚举值就是对外的错误码 |
| 领域模型 | auth/models.py | RuntimeAuthCheckResult（新） | 认证结果：成功时带会话，失败时带失败原因 |
| 仓储 | auth/infrastructure/sqlite_auth_repository.py | SqliteRuntimeAuthRepository.touch_session（改） | 同时写入 last_seen_at 和 expires_at |
| 领域服务 | auth/auth_service.py | RuntimeAuthService.check_token（新）、_renewed_expires_at（新）、authenticate_token（改为包装） | 有效性判定、失败归类、续期计算 |
| 适配器 | auth/north_gateway/auth_rest_adapter.py | RuntimeAuthMeResponse.code（新字段） | /me 未认证时返回失败码 |
| 适配器 | communication/north_gateway/communication_rest_adapter.py | create_communication_router（新增参数）、_format_sse_auth_invalid（新） | SSE 定期执行注入的校验，失败时推事件并关流 |
| 横切诊断 | runtime_network_monitor.py | RuntimeNetworkMonitor.admit_auth_failure_record（新）、snapshot 新增字段 | 认证失败日志的 60 秒窗口采样和省略计数 |
| 中间件 / 组合根 | server.py | runtime_auth_middleware、runtime_bootstrap_guard_middleware、network_monitoring_middleware（改）；_runtime_auth_failure_body（新）；_communication_stream_access_check（新闭包） | 401 响应体、只读校验、日志采样、向通信路由注入校验函数 |

**归属判定说明**：
- 续期和过期判定是业务规则，放在 RuntimeAuthService，与现有的密码策略、过期判定同处。
- 失败原因是有限的业务取值集合，放进领域模型。
- HTTP 响应体属于协议映射，放在 server.py 的中间件，和现有的 401 响应在同一位置。
- SSE 的复核节拍和帧格式属于通信协议，放在 communication 适配器。会话判定通过注入的函数完成，communication 不依赖 auth。
- 日志采样属于诊断横切，放进已有的全局单例 runtime_network_monitor。

### 5.2 技术架构
| 项 | 结论 | 依据 / 启用条件 | 方案要点 |
|---|---|---|---|
| 幂等与防重 | 不额外做 | 续期是覆盖写 | 并发续期时最后写入的生效，各次写入只差毫秒级 |
| 事务与一致性 | 单条 UPDATE | 只涉及一张表的一行 | `UPDATE … SET last_seen_at=?, expires_at=? WHERE session_id=? AND revoked_at IS NULL`，已撤销的会话不会被写回 |
| 缓存 | 不做 | 没有量化依据 | — |
| 写入性能 | 不变 | 现在每个认证请求本来就有一次 touch，只是同一条 UPDATE 多写一列 | bootstrap guard 改为只读校验，每个请求反而少一次写 |
| 存储容量 | 不做清理 | 会话行数等于登录次数，量很小 | 列入本次不做 |
| 容错降级 | 不做 | 只依赖本机 SQLite | — |
| 实时通信 | 做（D2） | EventSource 拿不到握手时的 401；已建立的流目前不复核会话 | 每 15 秒复核一次，对齐 keepalive 节拍，消息不断的流也会被覆盖。失败时推 communication.auth_invalid，然后结束响应 |
| ID | 不涉及 | — | — |
| 定时任务 | 不新增 | — | — |
| 安全 | 做 | D1 设定的绝对上限 | token 不变；泄露的 token 最长有效 90 天；日志里不出现 token；SSE 复核只读、不续期，长连接不能把会话一直撑下去 |
| 可观测性 | 做（D3） | 现场 stderr 约 11 分钟写满 5 MB（用户提供） | 键为“方法 + 路径（不含 query）”，窗口 60 秒。窗口内第一条照常写 http.request.end 和 WARNING，之后的只计数；下一条完整记录带 suppressed_since_last_record。network.snapshot 新增累计字段 suppressed_auth_failure_records。键数上限 512，超出时淘汰最早的键。只采样认证中间件判出的失败，登录密码错误的 401 不在范围内 |
| 发布兼容 | 兼容 | 只新增字段，不改表结构 | 旧前端忽略 code。仍有效的旧会话在下一次请求时按新规则续期，上限是创建时间 + 90 天。已过期的会话不会复活，现场那条会话需要重新登录一次 |

**假设与待确认**：
- 停止轮询、收到 auth_invalid 后关闭 EventSource、回到登录页，这些都由前端设计负责。后端只能把每个接口的日志压到每分钟最多 1 条完整记录。
- 401 WARNING 在 stderr 增量里的占比没有核实，stderr 里同时还有 DEBUG 级的其他诊断日志。安装包和 HEAD 2cc2e8f5 是否一致也没有核实。
- SSE 复核和现有中间件一样，在事件循环上同步读 SQLite，每个打开的流每 15 秒读一次。打开的流有多少，没有实测。
- attach、/events 这类关键路径的 http.request.start 在请求开始时就写了，那时还不知道会不会 401，所以不在采样范围内。这部分频率取决于前端的退避。

### 5.3 执行步骤
路径相对 agentruntime/src/agent_runtime/，测试相对 agentruntime/。

**步骤 1｜修改 auth/models.py**
- 构件与全名：agent_runtime.auth.models.RuntimeAuthFailureReason、agent_runtime.auth.models.RuntimeAuthCheckResult
- 层与位置：领域模型，和 RuntimeDeviceSession 放在同一个文件。
- 内容：
  - `class RuntimeAuthFailureReason(str, Enum)`，取值：TOKEN_MISSING="auth_token_missing"、TOKEN_INVALID="auth_token_invalid"、SESSION_REVOKED="auth_session_revoked"、SESSION_EXPIRED="auth_session_expired"。
  - `@dataclass(frozen=True, slots=True) class RuntimeAuthCheckResult`，字段：session: AuthenticatedRuntimeSession | None；failure_reason: RuntimeAuthFailureReason | None。
- 接线：步骤 3、5、7 引用。
- 验收：可以 import；两个字段有且只有一个非空（由步骤 3 的测试覆盖）。
- 前置：无。

**步骤 2｜修改 auth/infrastructure/sqlite_auth_repository.py**
- 构件与全名：agent_runtime.auth.infrastructure.sqlite_auth_repository.SqliteRuntimeAuthRepository.touch_session
- 层与位置：仓储。
- 内容：签名改为 `touch_session(self, session_id: str, last_seen_at: str, expires_at: str) -> None`，SQL 为 `UPDATE runtime_device_sessions SET last_seen_at = ?, expires_at = ? WHERE session_id = ? AND revoked_at IS NULL`。表结构不变。
- 接线：只有步骤 3 的 check_token 调用。在 `with self._lock, self._connect()` 内提交。
- 验收：调用后两列都更新；对已撤销的会话调用，库里的值不变。
- 前置：无。

**步骤 3｜修改 auth/auth_service.py**
- 构件与全名：agent_runtime.auth.auth_service.RuntimeAuthService.check_token / .authenticate_token / ._new_session；模块函数 _renewed_expires_at
- 层与位置：领域服务。
- 内容：
  - 删除 DEFAULT_SESSION_TTL_DAYS，新增 SESSION_IDLE_TTL_DAYS = 30、SESSION_ABSOLUTE_TTL_DAYS = 90。
  - `check_token(self, token: str | None, *, renew: bool) -> RuntimeAuthCheckResult`。只取一次 now = _now()，然后按顺序判定：
    1. token 为空，返回 TOKEN_MISSING
    2. get_session_by_token_hash 返回 None，返回 TOKEN_INVALID
    3. revoked_at 非空，返回 SESSION_REVOKED
    4. _parse_datetime(expires_at) <= now，返回 SESSION_EXPIRED
    5. 账号为 None 或 status != active，返回 TOKEN_INVALID
    6. renew=False，返回原会话，不写库
    7. renew=True：new_expires = _renewed_expires_at(session, now)，调用 touch_session(id, now.isoformat(), new_expires.isoformat())，返回用 dataclasses.replace 替换了 last_seen_at 和 expires_at 的会话
  - `authenticate_token(token)` 保留原签名，实现改为 `return self.check_token(token, renew=True).session`。
  - `_renewed_expires_at(session: RuntimeDeviceSession, now: datetime) -> datetime` 返回 `min(now + 30d, _parse_datetime(created_at) + 90d)`。
  - `_new_session` 的 expires_at 改为 now + SESSION_IDLE_TTL_DAYS。
- 接线：被 server.py 的中间件和组合根闭包、auth_rest_adapter.me 调用。不抛异常，失败通过返回值表达。
- 验收：T1 全部通过。
- 前置：步骤 1、2。

**步骤 4｜修改 auth/north_gateway/auth_rest_adapter.py**
- 构件与全名：RuntimeAuthMeResponse；create_runtime_auth_router 内的 me
- 层与位置：REST 适配器。
- 内容：
  - RuntimeAuthMeResponse 新增字段 `code: str | None = None`。
  - me 改为 `result = auth_service.check_token(_bearer_token(authorization), renew=True)`。result.session is None 时返回 `RuntimeAuthMeResponse(authenticated=False, code=result.failure_reason.value)`；否则返回现有结构，code=None。
  - login、bootstrap、logout、status 不改。
- 接线：/api/auth/* 不经过中间件认证，/me 自己完成认证和续期。
- 验收：T1 里 /me 的两个用例通过。
- 前置：步骤 3。

**步骤 5｜修改 runtime_network_monitor.py**
- 构件与全名：agent_runtime.runtime_network_monitor.RuntimeNetworkMonitor.admit_auth_failure_record / .snapshot
- 层与位置：横切诊断，放在已有的全局单例上。
- 内容：
  - 常量 AUTH_FAILURE_LOG_SAMPLE_WINDOW_SECONDS = 60.0、AUTH_FAILURE_LOG_SAMPLE_MAX_KEYS = 512。
  - `__init__` 新增 `self._auth_failure_windows: OrderedDict[str, tuple[float, int]]`（窗口起点，窗口内省略数）和 `self._suppressed_auth_failure_records = 0`。
  - `admit_auth_failure_record(self, key: str, *, now: float | None = None) -> tuple[bool, int]`，在 self._lock 内执行，t = now 或 time.monotonic()：
    - 键不存在，或 t - 起点 >= 60：prev = 旧窗口的省略数（没有则为 0）；写入 (t, 0) 并 move_to_end；长度超过 512 时 popitem(last=False)；返回 (True, prev)。
    - 否则：窗口省略数 +1，_suppressed_auth_failure_records +1，返回 (False, 0)。
  - snapshot() 的返回值新增 `"suppressed_auth_failure_records": self._suppressed_auth_failure_records`。
- 接线：步骤 7 的 network_monitoring_middleware 调用。快照线程不用改，自动带上新字段。
- 验收：T3 通过；现有 test_communication_sse_contract.py 不回归。
- 前置：无。

**步骤 6｜修改 communication/north_gateway/communication_rest_adapter.py**
- 构件与全名：CommunicationStreamAccessChecker（类型别名）、create_communication_router、_format_sse_auth_invalid
- 层与位置：通信 REST/SSE 适配器。
- 内容：
  - 常量 COMMUNICATION_SSE_AUTH_INVALID_EVENT_TYPE = "communication.auth_invalid"、COMMUNICATION_SSE_AUTH_RECHECK_SECONDS = 15。
  - `CommunicationStreamAccessChecker = Callable[[Request], tuple[str, str] | None]`，返回 (code, detail)；返回 None 表示通过。
  - 签名改为 `create_communication_router(gateway, *, stream_access_checker: CommunicationStreamAccessChecker | None = None)`。
  - event_generator 进入循环前设 `last_access_check = time.monotonic()`。每一轮在 is_disconnected 判定之后、处理 done 之前：如果 stream_access_checker 非空且距上次复核满 15 秒，就更新 last_access_check 并调用 checker。checker 返回非空时：close_reason = "auth_invalid"，`yield _format_sse_auth_invalid(...)`，然后 return，由已有的 finally 清理订阅并写 sse.close。
  - `_format_sse_auth_invalid(*, stream_id: str, code: str, detail: str) -> str` 输出 `event: communication.auth_invalid\ndata: {json}\n\n`。json 包含 stream_id、code、detail、server_time（UTC ISO），没有 id 行。
- 接线：checker 由步骤 7 的组合根注入；communication 包不 import auth。
- 验收：T2 通过。stream_access_checker=None 时行为和现在完全一样，原有 SSE 用例全部通过。
- 前置：无。

**步骤 7｜修改 server.py**
- 构件与全名：agent_runtime.server._runtime_auth_failure_body（新）；create_app 内的 runtime_auth_middleware、runtime_bootstrap_guard_middleware、network_monitoring_middleware；新闭包 _communication_stream_access_check
- 层与位置：中间件和组合根。
- 内容：
  - 模块常量 `_RUNTIME_AUTH_FAILURE_DETAIL = "未登录或登录已过期，请重新登录。"`。
  - `_runtime_auth_failure_body(reason: RuntimeAuthFailureReason) -> dict[str, str]`，返回 {"detail": _RUNTIME_AUTH_FAILURE_DETAIL, "code": reason.value}。
  - 认证中间件：
    - 无账号分支的 401 body 加上 "code": "auth_bootstrap_required"，并设 request.state.runtime_auth_failure_code = "auth_bootstrap_required"。
    - 有账号分支改为 `result = runtime_auth_service.check_token(token, renew=True)`。失败时设 request.state.runtime_auth_failure_code = result.failure_reason.value，返回 `JSONResponse(_runtime_auth_failure_body(result.failure_reason), status_code=401)`；成功时 request.state.runtime_auth_session = result.session。
  - bootstrap guard：把 `authenticate_token(...) is None` 改为 `check_token(..., renew=False).session is None`。
  - 网络监控中间件，在正常完成分支：
    - `auth_code = getattr(request.state, "runtime_auth_failure_code", None) if response.status_code == 401 else None`。
    - auth_code 非空时，调用 `admit_auth_failure_record(f"{request.method} {_redact_codex_network_path(request.url.path)}")`：
      - 返回 False：不写记录也不写日志，只设置 x-network-trace-id 后返回。
      - 返回 True：http.request.end 额外带 auth_failure_code 和 suppressed_since_last_record；WARNING 格式串末尾追加 `auth_failure_code=%s suppressed_since_last_record=%s`。
    - auth_code 为空时保持原逻辑。
  - 组合根：在 create_app 内定义 `_communication_stream_access_check(request) -> tuple[str, str] | None`。无账号时返回 None。否则 `result = runtime_auth_service.check_token(_runtime_auth_token_from_request(request), renew=False)`，失败时返回 (result.failure_reason.value, _RUNTIME_AUTH_FAILURE_DETAIL)，否则返回 None。
  - `create_communication_router(communication_gateway)` 改为 `create_communication_router(communication_gateway, stream_access_checker=_communication_stream_access_check)`。
- 接线：request.state 存在 ASGI scope 上，所以内层认证中间件写入的值，外层的监控中间件能读到。
- 验收：T4 通过；已有测试不回归（见步骤 9）。
- 前置：步骤 3、5、6。

**步骤 8｜新建/修改测试**
- 构件与全名：
  - 新建 tests/test_runtime_auth_session_lifecycle.py（T1）
  - 修改 tests/test_communication_sse_contract.py（T2）
  - 新建 tests/test_runtime_network_auth_failure_sampling.py（T3）
  - 修改 tests/test_runtime_auth_token_transport.py（T4）
- 层与位置：pytest，tests/。
- 内容：
  - T1，用 monkeypatch 替换 auth_service._now 来固定时间，覆盖：
    - 续期把过期时间推到 now + 30 天
    - 续期被 created + 90 天封顶
    - 过期返回 SESSION_EXPIRED，且库里的 expires_at 不变（不复活）
    - 撤销返回 SESSION_REVOKED；无 token 返回 TOKEN_MISSING；未知 token 返回 TOKEN_INVALID
    - renew=False 不写库
    - 用 TestClient 挂 create_runtime_auth_router：/me 未认证时返回 code，认证成功时返回续期后的 expires_at
  - T2：
    - 注入返回 ("auth_session_expired", "…") 的 checker，monkeypatch adapter.time.monotonic 让时间越过 15 秒；断言生成器产出 communication.auth_invalid 帧后结束，且 subscription.closed 为 True
    - 未满 15 秒时 checker 一次也没被调用
    - 帧里没有 id:
  - T3：
    - 第一条放行
    - 60 秒内同一个键被省略，累计数增加
    - 满 60 秒后再次放行，并返回上一窗口的省略数
    - 不同的键互不影响
    - 第 513 个键进来时淘汰最早的键
    - snapshot() 含 suppressed_auth_failure_records
  - T4：_runtime_auth_failure_body 对四种原因都返回固定的 detail 和对应的 code。
- 接线：无。
- 验收：四个测试文件全部通过。
- 前置：步骤 1~7。

**步骤 9｜验证**
- 构件与全名：先跑 `python -m pytest tests/test_runtime_auth_session_lifecycle.py tests/test_communication_sse_contract.py tests/test_runtime_network_auth_failure_sampling.py tests/test_runtime_auth_token_transport.py tests/test_memory_communication_phase1.py -q`，再跑完整的 `python -m pytest -q`。
- 层与位置：在 agentruntime/ 下执行。
- 内容：dev 手工验证，用 `python -m agent_runtime.desktop_server --config-file config/agent_runtime.yaml --env-file config/.env.dev` 启动：
  - 无 token 请求 /api/lacp/status，返回 401，code=auth_token_missing。
  - 连续请求期间，network-backend.jsonl 每 60 秒只有 1 条 http.request.end，network.snapshot 的计数在增长。
- 接线：无。
- 验收：全部测试通过，手工验证结果符合上面两条。
- 前置：步骤 8。

## 6. 前端技术架构（含代码架构）与执行清单

### 执行方案
- 任务：前端统一感知 Runtime 会话失效（REST 401 带 auth_ code、心跳 401、通信流 SSE 的 communication.auth_invalid、/me 返回的 code），之后清 Token 和 query 缓存，回到登录卡片并提示，停止全部轮询、心跳和重连，重新登录后恢复 Chat。
- 设计档位：标准设计。判据：跨模块（shared/api、shared/runtime、features/chat），接入新契约字段，改动认证门禁。
- 项目形态：老项目，首次接手（没有 .arch-memory/）。
- 架构基线（照源码归纳，用户已确认）：代码按 src/features/<m> 和 src/shared/ 组织。请求统一走 shared/api/httpClient.requestJson，DTO 类型写在 features/chat/api/agentRuntimeApi.ts。服务端状态用 TanStack Query（shared/api/queryClient），客户端状态用 zustand。认证门禁是 shared/runtime/RuntimeAuthGate，mode!=="ready" 时不渲染子树。Token 存在 localStorage，由 shared/api/runtimeAccess 读写。错误以 HttpError(message,status,body) 往上抛。测试用 scripts/*.test.mjs（node --test 加 vite ssrLoadModule）。
- UI：跳过。不新增页面和交互，过期提示复用现有登录卡片的 errorMessage 区域。
- 核心思路：失效后走现有“退出登录”同一条路径。所有失效信号汇总到一个共享通知点，只触发一次。Gate 收到后切到 login，AppShell 和 ChatPage 随之卸载，靠现有的卸载清理关掉 SSE、停掉心跳、清掉恢复定时器，query 观察者卸载后轮询也自然停止。这样不需要逐个修改 LACP、Git、Logs、Codex 的轮询入口。

### 一、业务映射
- 模块与域的对应：前端 shared/runtime（RuntimeAuthGate）和 shared/api 对应后端 auth 域；features/chat 的通信流对应后端 communication 域。
- 展示规则来源：失效判定以后端返回的 code 为准。前端不根据 expires_at 计算是否过期，也不做倒计时（依据 agentruntime:AUTH-04、agentruntime:COMMUNICATION-01）。
- 前端需求约束：
  - RUNTIME-AUTH-01：收到 Runtime 会话失效信号后，前端必须清除本地 Runtime Token 并回到登录态；登录态下不得再发起需要鉴权的轮询、心跳或重连。依据 agentruntime:AUTH-04、agentruntime:COMMUNICATION-01。
  - RUNTIME-AUTH-02：会话失效、退出登录、重新登录都只释放 scene presence（detach），不得停止 Agent 任务。

### 二、代码架构
| 层 / 文件 | 构件 | 职责 |
|---|---|---|
| shared/api/runtimeAuthInvalidation.ts（新） | runtimeAuthInvalidCodeFromBody、notifyRuntimeAuthInvalid、subscribeRuntimeAuthInvalid、isRuntimeAuthInvalidError | 失效判定与单点通知。按请求发出时使用的 Token 去重；如果那个 Token 已经不是当前 Token（说明已重新登录），忽略这次通知 |
| shared/api/httpClient.ts（改） | requestJsonWithMeta | 请求前记下当时的 Token；响应非 2xx 且命中判定时先通知，再照常抛 HttpError |
| shared/api/sseClient.ts（改） | SseClientOptions.terminalEventTypes、onTerminalEvent | 通用终止事件：收到后先 close()（closedByUser 置为 true，不再重连），再执行回调 |
| shared/api/queryClient.ts（改） | retry 函数 | 会话失效类错误不重试，其余保持 retry:1 |
| features/chat/api/agentRuntimeApi.ts（改） | sceneHeartbeat、createAgentCommunicationClient、RuntimeAuthMeResponse.code | 心跳遇到 401 时读取 body 并通知，返回值仍为 "error"；通信流 SSE 把 communication.auth_invalid 注册为终止事件，收到后通知 |
| shared/runtime/RuntimeAuthGate.tsx（改） | 订阅 effect、handleRuntimeAuthInvalid、refreshRuntimeAuth、logout | 失效时清 Token 和 me，cancelQueries 后 clear，写入提示，切到 login；遇到 auth_bootstrap_required 时改调 refreshRuntimeAuth()；/me 返回 false 时清 Token；logout 补上清 query 缓存 |

状态归属：
- Token：只在 runtimeAccess（localStorage），沿用现状。
- 认证 mode 和错误提示：Gate 的局部状态。
- 失效通知：模块级订阅集合，本身不存数据。
- chat 的 zustand store：保持现状，不清空。重新挂载时由 ChatPage 现有的 attach/hydrate 流程重建。

### 三、技术架构
| 项 | 结论 |
|---|---|
| 请求与缓存 | 失效时先 cancelQueries 中止在途请求，再 clear() 清缓存，避免换账号后看到上一个账号的数据。失效错误不重试 |
| 流式传输 | 通信流 SSE 收到 communication.auth_invalid 后主动 close，不进入自动重连。上传进度 SSE 只能通过握手 401 体现，而 EventSource 读不到状态码，所以它会继续退避重连，直到 Gate 切换导致组件卸载为止。REST 轮询或心跳会先发现失效，这段重连窗口很短 |
| 实时 / 心跳 | 心跳是直接 fetch，绕过了 httpClient；它的 401 也接入同一个通知点，补上这个缺口 |
| 内存与资源释放 | 依赖现有卸载清理：useSceneCommunication 卸载时关闭 SSE、停止心跳、清理定时器和 cursor；query 观察者卸载后 interval 停止。新增的订阅在 effect cleanup 中退订 |
| 恢复 | 重新登录后 Gate 回到 ready，ChatPage 重新挂载，走现有的 attach 加 chat bubble hydrate。cursor 在卸载时已经清掉，所以恢复时整段补水，不按旧的 after_seq 续传，不会出现假缺口 |
| 安全 | 失效后立即删除 localStorage 里的 Token。login 密码错误返回的 401 不带 code，不会被误判为会话失效 |
| 可观测性 | 不改。轮询停止后，rest.http_error 只剩失效时刻的少量记录 |
| 三端一致 | 改动全部在共享层（shared/api、RuntimeAuthGate），Electron、desktop-web、mobile-web 同时生效；Electron main 和 preload 不改 |
| 渲染、长列表、富文本、大文件、本地存储、加载性能 | 不涉及 |

### 四、契约使用
- 用到的接口：
  - 所有受保护 REST 的 401 响应 {detail, code}；判定条件为 status===401 且 code 以 auth_ 开头。
  - GET /api/auth/me 返回的 code。
  - 通信流 SSE 事件 communication.auth_invalid，字段为 {stream_id, code, detail, server_time}，没有 id 行。
- 契约修改请求：无。

### 五、执行清单
施工顺序：api → 门禁 → 测试。路径相对 agent-frontend/。

1. 新建 src/shared/api/runtimeAuthInvalidation.ts
   - `type RuntimeAuthInvalidCode = "auth_token_missing"|"auth_token_invalid"|"auth_session_revoked"|"auth_session_expired"|"auth_bootstrap_required"`
   - `type RuntimeAuthInvalidSignal = { code: RuntimeAuthInvalidCode; detail: string | null; source: "rest"|"heartbeat"|"sse"|"me" }`
   - `runtimeAuthInvalidCodeFromBody(status: number, body: unknown): RuntimeAuthInvalidCode | null`：仅当 status===401、body 是对象、body.code 是以 "auth_" 开头的字符串时返回该 code，否则返回 null。
   - `notifyRuntimeAuthInvalid(signal, sentToken: string | null): void`：
     - sentToken 不等于 getRuntimeAccessToken() 时直接返回。
     - 同一个 sentToken 只通知一次，用模块级变量 lastNotifiedToken 记录。
     - 同步调用所有监听器。
   - `subscribeRuntimeAuthInvalid(listener: (s: RuntimeAuthInvalidSignal) => void): () => void`
   - `isRuntimeAuthInvalidError(error: unknown): boolean`：error 是 HttpError 且 runtimeAuthInvalidCodeFromBody(error.status, error.body) 非空。
   - 依赖方向：本文件从 httpClient 引入 HttpError；httpClient 只引用本文件的 runtimeAuthInvalidCodeFromBody 和 notify，两边没有顶层求值依赖。如果类型检查报循环依赖，就把 HttpError 移到 shared/api/httpError.ts，由 httpClient 重新导出。
   - 验收：T1 通过。
2. 改 src/shared/api/httpClient.ts
   - 构造 headers 之前取 `const sentToken = getRuntimeAccessToken()`。
   - 在 !response.ok 分支 throw 之前插入：`const code = runtimeAuthInvalidCodeFromBody(response.status, body); if (code) notifyRuntimeAuthInvalid({ code, detail: message, source: "rest" }, sentToken)`。
   - 其余行为不变。
3. 改 src/shared/api/sseClient.ts
   - SseClientOptions 新增 `terminalEventTypes?: string[]` 和 `onTerminalEvent?: (eventType: string, data: unknown) => void`。
   - connect() 中为每个终止事件类型 addEventListener，处理顺序：先尝试 JSON 解析 data（失败则保留原字符串），再 this.close()，最后调用回调。
   - 终止事件不进入 onEvent，也不计入 messageCount。
4. 改 src/shared/api/queryClient.ts
   - `retry: (failureCount, error) => !isRuntimeAuthInvalidError(error) && failureCount < 1`
5. 改 src/features/chat/api/agentRuntimeApi.ts
   - RuntimeAuthMeResponse 增加 `code?: string | null`。
   - sceneHeartbeat：
     - fetch 之前取 sentToken。
     - response.status===401 时，读取 text 并做 JSON 解析；命中判定就调用 notify（source 为 "heartbeat"），然后返回 "error"。
     - 解析失败时 console.warn，返回 "error"，不要写空 catch。
     - 404 仍然返回 "expired"。
   - 新增常量 `COMMUNICATION_SSE_AUTH_INVALID_EVENT_TYPE = "communication.auth_invalid"`。
   - createAgentCommunicationClient：传入 `terminalEventTypes: [该常量]`。onTerminalEvent 从 data 中取 code 和 detail，code 以 auth_ 开头时调用 notify（source 为 "sse"，sentToken 取当前 Token）。
6. 改 src/shared/runtime/RuntimeAuthGate.tsx
   - 新增 `useEffect(() => subscribeRuntimeAuthInvalid(handleRuntimeAuthInvalid), [handleRuntimeAuthInvalid])`。
   - handleRuntimeAuthInvalid(signal) 用 useCallback 定义：
     - code 为 auth_bootstrap_required 时，调用 void refreshRuntimeAuth()。
     - 其他 code 依次执行：setRuntimeAccessToken(null)、setMe(null)、void queryClient.cancelQueries()、queryClient.clear()、setErrorMessage(signal.detail ?? "登录已过期，请重新登录。")、setMode("login")。
   - refreshRuntimeAuth：/me 返回 authenticated===false 时也执行 setRuntimeAccessToken(null)；code 为 auth_session_expired 或 auth_session_revoked 时，进入 login 后设置提示“登录已过期，请重新登录。”。
   - logout 的 finally 中补上 queryClient.cancelQueries() 和 queryClient.clear()。
   - queryClient 从 @/shared/api/queryClient 引入。
   - 验收：T2 和手工 M1~M3 通过。
7. 测试：新建 scripts/runtime-auth-invalidation.test.mjs
   - 沿用现有写法：node --test 加 vite ssrLoadModule；stub window.localStorage、fetch、EventSource。
   - package.json 增加 `"test:runtime-auth": "node --test scripts/runtime-auth-invalidation.test.mjs"`。
   - 用例：
     - T1-a：收到 401 且 code=auth_session_expired，通知一次；连续 3 次这样的 401，仍只通知一次。
     - T1-b：401 不带 code（login 密码错误），不通知。
     - T1-c：sentToken 与当前 Token 不同，不通知。
     - T1-d：sceneHeartbeat 遇到带 code 的 401，返回 "error" 并通知；遇到 404，返回 "expired" 且不通知。
     - T1-e：SseClient 收到终止事件后调用 close，不再调度重连，回调拿到解析后的 data。
     - T1-f：isRuntimeAuthInvalidError 能区分带 code 和不带 code 的 401。
     - T2：Gate 的处理逻辑，用 stub 的 queryClient 验证 cancelQueries 和 clear 都被调用、Token 被清除。
8. 验证
   - 命令：npm run typecheck、npm run build、npm run test:runtime-auth，现有 test:chat-* 不得回归。
   - 手工验证（dev 环境，后端已按定稿实现）：
     - M1：登录后在 DB 里把会话的 expires_at 改成过去的时间。几秒内应回到登录卡片并显示提示，DevTools 中不再出现轮询或心跳请求。
     - M2：重新登录后 Chat 恢复 scene，Agent 任务没有被停止。
     - M3：login 输错密码，仍停留在登录卡片，提示“账号或密码错误。”。

## 7. 跨端施工顺序

1. 后端先按第 5 节执行清单落实第 4 节契约。
2. 前端按第 6 节“api → 门禁 → 测试”顺序落实；手工验证的前置条件为“后端已按定稿实现”。
3. 两端各按设计师给出的验证清单验收。本任务仅交付设计文档与架构记忆，不执行这些编码或测试步骤。

## 8. 本次不做什么

### 后端设计师原文
refresh token 和 token 轮换；会话表结构变更；过期会话清理任务；上传进度 SSE 的复核（短任务，到终态即结束）；无账号时 403 local_bootstrap_required 的 body；登录密码错误的 401 body（保持现状，不带 code）；uvicorn access log；Electron network-electron.jsonl 的写入策略；所有前端改动（由前端设计负责）。

### 前端设计师原文
- 新的过期 UI、过期前提醒、倒计时。
- 把 Token 从 localStorage 移到内存。skill 基线建议长期凭证不存 localStorage，但这会牵涉三端刷新后保持登录的语义，需要单独立项。
- ChatPage 卸载时 detach beacon 携带过期 Token 产生的 401：每个 scene 一次，presence 会在 120 秒后按 stale 被回收。
- Electron main 代理和 network-electron.jsonl 的写入策略。
- 多标签页同步（BroadcastChannel）：其他标签页会在各自下一次 401 时自行回到登录态。

## 9. 记忆变更清单：按仓库分组

### 六、记忆影响（仓库 agentruntime，.arch-memory/ 新建；设计定稿模式）
| 文件 | 动作 | 条目原文 | 依据 |
|---|---|---|---|
| tech/overview.md 架构基线 | 新增 | 分层：按模块分包 agent_runtime/<模块>/，模块内为领域服务、models、infrastructure 仓储、north_gateway REST 适配器、factory 组合根，无独立 app 层 / 命名：*Service、Sqlite*Repository、create_*_router、*Factory，REST 入参 *Body、出参 *Response / 逻辑承载：业务规则写在领域服务，REST 适配器只做 HTTP 映射与限流 / 跨模块与横切：server.create_app 为全局组合根，认证、bootstrap、网络监控以 HTTP middleware 写在 server.py / 持久化：sqlite3 手写 SQL，每次操作独立连接并提交，表结构在连接时以 CREATE TABLE IF NOT EXISTS 保证，行转 frozen dataclass / 错误：领域服务抛 ValueError，由适配器或中间件转为 HTTP 响应 | 用户确认基线（“全部按建议，D3 把省略计数放进 snapshot”；终审“通过，按此定稿（含规范性条目）”） |
| tech/overview.md 全局技术约定 | 新增 | TECH-01 认证中间件判定失败的 401 请求诊断日志（network-backend.jsonl 与 stderr）每个“方法 + 路径”每 60 秒仅限 1 条完整记录，其余只计入 network.snapshot 的 suppressed_auth_failure_records 累计数。 | D3，用户原话同上 |
| business/overview.md | 新增 | 领域清单：\| auth \| 支撑 \| 访问本机 Runtime 的本地账号、登录会话与认证判定 \| domains/auth.md \|；\| communication \| 支撑 \| 业务无关的逻辑通信流、消息持久化与 SSE 推送 \| domains/communication.md \|。域间关系：communication → auth：同步，通信流 SSE 按 COMMUNICATION-01 复核登录会话 | 本方案（描述性） |
| business/domains/auth.md | 新增 | 边界：不负责云账号与跨 Runtime 同步，不负责远程访问通道。关键概念：登录会话是登录或首次初始化创建的设备会话，凭 Bearer token 访问 Runtime。状态流转：有效 →(空闲超过 AUTH-01 时限或到达 AUTH-02 上限) 已过期；有效 →(登出) 已撤销。需求约束：AUTH-01、AUTH-02、AUTH-03、AUTH-04（正文见第一节） | D1；用户原话同上 |
| business/domains/communication.md | 新增 | 边界：不负责登录会话判定（→ auth）。需求约束：COMMUNICATION-01（正文见第一节） | D2；用户原话同上 |
| tech/designs/auth.md | 新增 | ## 会话续期 / 方案：续期沿用原 token，不提供刷新接口 / 不变量：AUTH-01 的续期仅限认证中间件与 /api/auth/me 的认证成功触发；bootstrap 守卫与通信流 SSE 复核仅限只读校验 / 禁止：不得为 AUTH-02 的上限新增会话表字段，上限由会话创建时间推导 | 后端定稿终审，用户原话“通过，按此定稿（含规范性条目）”；文档终审，用户原话“文档通过，写入记忆并提交” |
| tech/designs/communication.md | 新增 | ## 通信流会话复核 / 方案：COMMUNICATION-01 的会话复核由 Runtime 组合根注入校验能力，communication 不依赖 auth / 禁止：communication.auth_invalid 事件帧不得携带 id，不得推进业务 seq | 同上 |

- **规范性变更（用户已显式确认）**：架构基线 6 项；AUTH-01~AUTH-04；COMMUNICATION-01；TECH-01。前端仓库引用时写 `agentruntime:AUTH-04`、`agentruntime:COMMUNICATION-01`，写入时会分配正式编号，届时替换。
- **记忆与源码冲突**：没有（记忆还不存在）。与原问题报告不一致的有两处：/api/runtime-bootstrap/ 不在认证白名单内；认证 401 的 detail 文案不会命中前端的 fatalRestoreError 正则。
- structure.md 由 arch-memory-update 根据执行清单推导。

### 七、记忆影响（仓库 agent-frontend，.arch-memory/ 新建，设计定稿模式）
| 文件 | 动作 | 条目原文 | 依据 |
|---|---|---|---|
| tech/overview.md 架构基线 | 新增 | 即“执行方案”中架构基线一条的原文 | 用户终审“通过，按此定稿” |
| business/overview.md 领域清单 | 新增 | \| runtime-auth \| 支撑 \| 前端对本机 Runtime 登录会话的感知与登录态切换：Runtime Token 的本地存取，会话失效信号的统一感知（判定以后端返回的 code 为准），以及回到登录态；对应后端 agentruntime 的 auth 域，通信流 SSE 的失效事件对应后端 communication 域 \| domains/runtime-auth.md \| | 已确认定稿“一、业务映射”（用户原话“通过，按此定稿”） |
| business/domains/runtime-auth.md | 新增 | RUNTIME-AUTH-01 收到 Runtime 会话失效信号后，前端必须清除本地 Runtime Token 并回到登录态，登录态下不得再发起需要鉴权的轮询、心跳或重连（依据 agentruntime:AUTH-04、agentruntime:COMMUNICATION-01）。RUNTIME-AUTH-02 会话失效、退出登录、重新登录只释放 scene presence（detach），不得停止 Agent 任务。 | 同上 |
| tech/designs/runtime-auth.md | 新增 | ## 会话失效处理 / 方案：仅限后端判定为 Runtime 会话失效的信号（REST 与心跳中 code 以 auth_ 开头的 401、通信流 SSE 失效事件，判定依据 agentruntime:AUTH-04；不带 code 的 401 不在此列）统一汇聚到一个共享失效通知点，由认证门禁统一切换登录态；轮询、心跳、重连随门禁子树卸载一并停止，不逐个改造轮询入口（依据 RUNTIME-AUTH-01） / 不变量：失效通知以请求发出时携带的 Runtime Token 为去重键，同一 Token 只触发一次 / 禁止：业务组件与 feature 不得自行根据 401 清除 Runtime Token 或切换登录态 | 前端定稿用户原话“通过，按此定稿”；组装文档用户原话“文档通过，写入记忆并提交” |

- 跨仓引用：agentruntime:AUTH-04、agentruntime:COMMUNICATION-01 都是后端方案中的占位编号，写入时请替换为正式编号。
- 记忆与源码冲突：无，记忆目前不存在。

## 10. 架构记忆更新报告

模式：设计定稿。实际写入者：系统设计编排 Agent。依据为用户确认的设计定稿；这一轮只纠正记忆条目的内容准入问题，不重新评审设计，也不执行代码落地校准。

### agentruntime
状态：已更新。

变更：
- tech/designs/auth.md：照录后端设计师等义修整后的方案、不变量、禁止项；去掉具体列名和字段写入动作，保留续期入口、只读校验范围、不换 token、不提供刷新接口和不新增上限字段的决策。
- tech/designs/communication.md：照录后端设计师等义修整后的组合根注入决策和协议禁止项；去掉具体函数接线，保留对外事件名、id、seq 限制。
- 其余架构记忆文件及 AUTH-01、AUTH-02、AUTH-03、AUTH-04、COMMUNICATION-01、TECH-01 均保持不变。

退回及处理：
- 写入前，含字段写入动作、具体方法和函数接线的两份技术记忆整体退回给原后端设计师；返回合格条目前不作这一轮记忆写入。
- 后端设计师提供完整正文、清单与逐条等义说明，明确“规范意义没变”，没有修改方案、源码或文件；编排重新校验后原文写入。

### agent-frontend
状态：已更新。

变更：
- tech/designs/runtime-auth.md：照录前端设计师修整后的单点通知、门禁子树卸载、请求 Token 去重及禁止业务组件自行退登录的条目，移除具体代码路径与收到消息后的逐步操作表述。
- 保留带 auth_ 错误码的 401 这一适用前提；不带 code 的 401 仍明确排除，不改变已批准的错误判别范围。
- 其余架构记忆文件及 RUNTIME-AUTH-01、RUNTIME-AUTH-02 均保持不变。

退回及处理：
- 写入前，原技术记忆的代码路径、具体接线及操作流程表述退回给原前端设计师。
- 第一次修整遗漏原有 auth_ code 前提，按“准确保留适用范围、前提、例外”再次退回；没有把该不合格版本写入文件。
- 第二次修整恢复原有适用前提。设计师明确“方案本身和能力范围都没动”，编排校验通过后原文写入。

### 编号与跨仓引用
- 本轮不分配新编号；后端正式编号 AUTH-01～AUTH-04、COMMUNICATION-01、TECH-01，前端正式编号 RUNTIME-AUTH-01、RUNTIME-AUTH-02 均沿用。
- 跨仓引用仍为 agentruntime:AUTH-04、agentruntime:COMMUNICATION-01；只在前端本仓规则句内引用，不复制后端规则正文。
- 本轮不新增领域、机制或结构映射，不修改已确认架构基线。

### 校验范围与结果
- 内容准入：逐条核对三份修正文件的依据、适用前提、强度、唯一来源、放置位置和写作要求；内容问题先退回，再校验，不以编号或路径检查替代内容检查。
- 规范依据：原后端方案确认“通过，按此定稿（含规范性条目）”、原前端方案确认“通过，按此定稿”、文档终审“文档通过，写入记忆并提交”；两位设计师均提供规范意义不变的说明。
- 写入核对：修正正文与设计师返回原文一致；正式编号及跨仓引用有效，无占位编号、空标题、设计规范引用或计划措辞；篇幅和修改范围检查通过。
- 本轮受影响条目无未处理的退回项，无新增冲突或待澄清项。该结论仅针对本轮记忆表述修正，不是对业务实现或整个设计的重新验收。
- 先前写入把机械检查结果概括为完整合规结论，不够准确；由本报告替换，前次提交保留在 Git 历史中。
- 设计文档第 0～8 节、业务源码、测试、配置与正式版数据不在本轮修改范围；这次修正另作提交，不改写既有提交历史。
