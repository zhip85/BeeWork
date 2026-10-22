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
