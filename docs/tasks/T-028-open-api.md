# T-028 租户 API 密钥与开放对话 API

**阶段**：3/6（试点版对外入口）　**依赖**：T-024、T-025、T-010　**PRD**：SET-090、NFR-017、RUL-021（QPS）、PLT-084、4.12 ApiKey、5.5 第 1–2 步

## 范围

1. API 密钥（SET-090）：`tenant_api_keys`（只存哈希与掩码，`bw_live_`/`bw_test_` 前缀 + 24 位随机串，环境、权限范围、过期时间、最近使用时间）；“设置 → API 与 Webhook → API 密钥”页：创建（明文只显示一次）、列表、吊销（确认后立即失效）。成员被暂停时吊销其创建的密钥（SET-013）。
2. 开放 API（独立路由前缀 `/v1`，与控制台 `/api/v1` 分开）：
   - 认证：`Authorization: Bearer bw_...`；按密钥确定租户与环境，权限范围需含 `chat`。
   - `POST /v1/assistants/{assistant_id}/chat`：请求 `{message, conversation_id?, end_user: {id, name?}, stream?}`；生产环境使用线上版本，开发/预发使用最新草稿（RUL-003）；`stream=true` 返回 SSE。
   - `GET /v1/conversations/{id}/messages`。
   - 限流：按租户 QPS（订阅配额）执行，超限 429 + `Retry-After`（Valkey 令牌桶）。
   - 错误：统一 `{error: {code, message}}`；OpenAPI 文档单独分组，路径版本化。
3. 助理详情“渠道”页签中开放 API 卡的“接入说明”（接口地址、鉴权、请求/响应示例）。

## 验收标准

- [ ] 吊销后立即 401；过期密钥 401；无 `chat` 范围 403。
- [ ] 跨租户：A 租户密钥访问 B 租户助理返回 404。
- [ ] 限流测试（PostgreSQL/Valkey 环境）。
- [ ] 用 curl 跑通一次真实对话（有供应商 Key 时），记录在 development-status。

## 完成记录

**改动文件**
- 后端：新增 `models/channels.py`（`TenantApiKey`，与 T-040 的 `ChannelInstance` 同文件）、迁移 `0027_channels_api_keys`（可降级）、`api_keys.py`（生成/哈希/认证/吊销）、`ratelimit.py`（令牌桶）、`channel_chat.py`（开放 API 与挂件共用的对话执行路径）、`schemas/open_api.py`、`api/api_keys.py`（控制台）、`api/open_api.py`（`/v1`）、`tests/test_open_api.py`（21 项）、`tests/test_postgres_integration.py`（真实 Valkey 令牌桶并发）、`scripts/check_open_api_e2e.py`；修改 `main.py`（挂载 `/v1`）、`api/conventions.py`（`/v1` 的错误信封）、`errors.py`、`api/members.py`（暂停/移除成员吊销其密钥）、`api/assistants.py`（`_enabled_chat_models` 可指定环境）、`models/__init__.py`（审计动作，`api_key.*` 归“开发者”类）。
- 前端：`pages/settings/ApiKeysPage.tsx`（设置 → API 密钥：创建、明文只显示一次、列表、吊销）、`pages/channels/OpenApiCard.tsx`（助理“渠道”页签里的接入说明）、`vite.config.ts`（开发代理 `/v1`）、`i18n/*/channels.json`。

**行为**
- 密钥：生产 `bw_live_`、其他环境 `bw_test_` + 24 位随机字符；只存 SHA-256 与掩码（前 8 位 + •••• + 后 4 位），明文仅在创建响应里出现一次；环境须是套餐提供的环境；权限范围 chat / knowledge.read / knowledge.write / analytics.read（至少一项，目前只有 `chat` 有接口）；可选过期时间；最近使用时间每分钟最多写一次。创建与吊销写审计（开发者类）。成员被暂停或移除时，其创建的密钥立即吊销（SET-013）。
- 开放 API（`/v1`，独立于 `/api/v1`）：`Authorization: Bearer bw_…`；`POST /v1/assistants/{id}/chat`（`message`、`conversation_id?`、`end_user{id,name?}`、`stream?`）与 `GET /v1/conversations/{id}/messages`（`after_seq`、`limit`、`has_more`）。生产环境密钥执行助理**已发布版本**，开发/预发密钥执行**最新草稿**（RUL-003），对应环境的网关 Key 与启用模型。`stream=true` 返回 SSE：`start` / `delta` / `done` / `error`，不暴露内部管线步骤。
- 错误统一 `{"error":{"code","message"}}`（认证 401、范围 403、跨租户与不存在 404、校验 422、限流 429、模型失败 502 且不泄露供应商细节、超时 504）；吊销与过期立即生效；他租户的助理/会话与不存在无法区分；控制台会话对开放 API 不可见；终端用户只能续接自己的会话。
- 限流：按租户订阅的 `api_qps` 做令牌桶（突发 = 1 秒额度），超限 429 + `Retry-After`；令牌桶在 Valkey（Lua、服务器时钟，多实例一致），Valkey 不可用时退回进程内令牌桶。
- 文档：独立的 OpenAPI 文档 `/v1/openapi.json` 与 `/v1/docs`，路径版本化。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + 全量 337 项）通过；前端检查通过。
- 单元/集成测试覆盖：创建与校验、明文只出现一次且只存哈希、权限与租户隔离、缺失/乱码/吊销/过期密钥、无 `chat` 范围 403、暂停创建者吊销密钥、停用租户不可用、最近使用、完整多轮对话（线上版本、版本标签、终端用户）、消息分页、SSE、生产跑线上版本而开发跑草稿、未发布/已停用/不存在/跨租户、会话归属、校验与未知路径的错误信封、模型失败 502、QPS 429 与恢复、令牌桶补充与隔离、独立 OpenAPI 文档。PostgreSQL 环境下 12 个并发请求经 4 个“实例”打同一个真实 Valkey 桶，恰好放行突发额度。
- **真实栈端到端**（`scripts/check_open_api_e2e.py`，本机 LiteLLM + Worker + Valkey，模型部署改为 `mock_response`，端点用公网 IP 通过 URL 校验）全部通过：用 HTTP 客户端经 `/v1` 完成真实对话（非流式与 SSE）、读取消息、吊销后立即 401。**没有任何真实供应商 Key，因此没有用真实模型回答验证过**。
- 同时修正了 `check_runtime_e2e.py`：它结束时直接删除已发布的助理，被 409 拒绝，每运行一次就在开发租户里遗留一个助理，最终占满套餐配额；现在先停用再删除，已清理开发库里遗留的 5 个。

**遗留问题**
- 验收项“用 curl 跑通一次真实对话（有供应商 Key 时）”：没有 Key，仅用 `mock_response` 验证；拿到 Key 后请用真实模型再跑一次并记录。
- IP 取自 `request.client.host`，部署在反向代理后需配置可信代理头（影响挂件的按地址限流）。
- `knowledge.*`、`analytics.read` 范围可以创建但暂无对应接口（T-094）；Webhook（SET-091）属 T-087，本页只做“API 密钥”。
- 非流式请求会占用一个服务线程等待 Worker 结果（超时 = 助理超时 + 30 秒），高并发时需要关注线程池。

**审查修正**
- 修复环境隔离：此前开发/预发（`bw_test_`）密钥可以读取、甚至续接生产环境的开放 API 会话（已用测试复现：读取返回 200）。现在读取消息与续接会话都按密钥所属环境过滤，跨环境一律 404；各自环境内不受影响。
- 修复 Valkey 故障时限流拖慢每个请求：原先每次请求都要等一次连接超时（1 秒）才退回进程内令牌桶。现在首次失败后 10 秒内直接用进程内令牌桶，之后再试一次 Valkey；测试覆盖“仍按限额放行”和“退避后重试”。
- `.env.example` 增加 `PUBLIC_API_URL`（反向代理或生产环境下需设置，否则接入代码里的地址取自请求地址）。
