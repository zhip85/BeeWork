# T-087 领域事件总线与 Webhook

**版本**：V0.3　**依赖**：T-002、T-071　**PRD**：3.10.10 Webhook（SET-091）、5.8、APR-025、SET-083、NFR-020

## 设计

- 事件总线 `beework/events.py`：`emit_event(db, tenant_id, type, payload)` 写 `domain_events`（同事务，outbox），Worker 分发给订阅者：站内通知规则（T-088）、Webhook 端点（本任务）。首批事件：`conversation.completed`（T-026）、`handoff.requested`（T-081）、`approval.updated`（T-022）、`usage.threshold`（T-011）、`assistant.published`（T-024）。已完成的任务在本任务中补上 `emit_event` 调用。
- 载荷只含 ID 与必要摘要，不含会话正文和密钥。

## 范围

1. 端点（SET-091）：地址（公网 https 校验）、订阅事件（≥ 1）、启用开关、签名密钥（只在创建时展示一次，加密存储，可轮换）。
2. 投递：POST JSON，Header 含事件名、投递 ID、时间戳、`HMAC-SHA256` 签名；经 T-071 安全出站客户端；非 2xx 或 10 秒超时为失败；按 1 / 5 / 30 分钟、之后每小时重试，最多 24 小时；连续失败 24 小时自动停用并通知租户管理员。
3. 投递记录：最近 100 次（状态码、耗时、错误、时间），“重发”与“测试”（发送测试事件并刷新最近推送）。
4. “设置 → API 与 Webhook → Webhook”页。

## 验收标准

- [ ] 测试：签名可由公开的验证示例代码复算；重试时间表（可注入时钟）；24 小时后停用；投递 ID 在重试中保持不变（接收方可去重）。
- [ ] 内网地址端点保存被拒。
