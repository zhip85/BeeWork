# T-026 会话与消息、保留期与脱敏

**阶段**：3　**依赖**：T-025　**PRD**：4.4 Conversation/Message/EndUser、SET-070、AST-072、8.3#18、NFR-010

## 范围

1. `conversations`、`messages`、`end_users` 表（按 PRD 4.4，均带 `tenant_id`、`environment`）；run 关联 `conversation_id`；多轮对话把历史消息作为 LangGraph 输入（短期窗口，条数可配置，长期记忆属于 T-056）。
2. 合规设置（SET-070 中的保留期、日志脱敏、允许导出三项）：`tenant_compliance_policies` 表与“设置 → 合规与审批 → 数据与隐私”表单；保留期不得超过套餐 `log_retention_days`。
3. 脱敏：开启“日志中脱敏个人信息”时，消息入库前用基础规则（手机号、邮箱、身份证、银行卡正则）脱敏；规则实现放 `beework/pii.py`，T-055 会替换为可配置规则 + Presidio。
4. Worker 每日按保留期删除过期会话与消息（写一条汇总审计）。
5. 会话结束判定：30 分钟无消息置为结束（8.3#18），定时任务处理。
6. 助理详情“会话记录”页签（AST-072 的列表与详情，导出功能等 T-022 的数据导出审批接入后再开）。

## 验收标准

- [ ] 脱敏单元测试；关闭脱敏时原文入库。
- [ ] 保留期清理有测试（可注入时钟）。
- [ ] 跨租户读取会话返回 404。

## 完成记录

**改动文件**
- 后端新增：`models/conversations.py`、`conversations.py`、`compliance.py`、`pii.py`、`errors.py`（`ApiError` 移出 `api/conventions`，后者仍重新导出，避免服务模块导入 API 包造成循环）、`schemas/conversations.py`、`api/conversations.py`、`api/compliance.py`、迁移 `0018_conversations.py`（可降级）、`tests/test_conversations.py`（20 项）。
- 后端修改：`runtime/executor.py`（历史、助理消息、内容擦洗）、`runtime/dsl.py`、`runtime/tracing.py`（Langfuse `mask`）、`api/runs.py`、`schemas/runs.py`、`worker/scheduler.py`、`models/__init__.py`、`api/__init__.py`、`entitlements.py`、`tests/test_runtime.py`、`tests/test_worker.py`、`scripts/check_runtime_e2e.py`（多轮 + 脱敏校验）。
- 前端：`pages/settings/DataPrivacyPage.tsx`、`SettingsTabs.tsx`、`pages/assistants/ConversationsPage.tsx`、`i18n/*/conversations.json` 与 `settings.json`，`App.tsx`、`AssistantsPage.tsx`、`App.css`。

**接口**：`GET/PUT /api/v1/tenant/compliance`；`GET /api/v1/assistants/{id}/conversations`（`status`、`started_from/to`、分页）；`GET /api/v1/conversations/{id}`；`POST /api/v1/assistants/{id}/runs` 增加 `conversation_id` 并返回 `conversation_id`。

**验证**
- `ruff`、`./scripts/test-postgres.sh`（迁移升降级 + `alembic check` + 125 项）、前端 `tsc`/`lint`/`i18n:check`/`build` 通过。
- 新测试覆盖：脱敏规则（含不误伤普通数字）、新会话与助理回复入库、续聊历史顺序与窗口上限、仅创建者可续聊/跨助理 404、并发消息 409、失败运行只留用户消息、默认脱敏（消息/run/事件）与关闭脱敏保留原文、合规策略默认值按套餐截断、保留期校验 422/乐观锁 409/审计/403、套餐上限优先于已存值、会话列表与详情的权限与跨租户 404、30 分钟结束（已答复→已解决，未答复→已放弃，进行中运行不结束）、新消息重开会话、结束钩子、保留期清理（含 run/事件/消息、每租户一条审计、可注入时钟）。
- 真实栈上的 `check_runtime_e2e.py`（`mock_response` 模型）：两轮对话入库 4 条消息 2 轮、消息中手机号已替换为 `[手机号]`，Langfuse 上的 trace 里读不到原始手机号。**仍是 mock 模型，未连真实供应商；检索仍是桩。**

**遗留问题**
- 质量分、满意度（csat）、渠道、用户国家/意图字段只建了列，没有写入逻辑（T-080/T-041）。
- AST-072 的“PII 按查看人权限脱敏”：目前只在存储时按租户策略脱敏，没有“查看人权限”维度；导出需审批的功能待 T-022/T-086（`allow_export` 开关已存，导出入口尚不存在）。
- 会话结束的状态规则（已答复→已解决，未答复→已放弃）是本任务按 PRD 8.3#18 暂定的实现，请产品确认。
- 脱敏只用固定正则；银行卡需通过 Luhn 校验，号码中夹空格/横线的写法不会命中。T-055 替换为可配置规则。
- 清理任务按租户每次最多删 1000 个会话，积压时下一天继续；没有跨日续跑。
- 已结束的会话收到新消息会自动重开；没有“会话过长”的消息数上限。
- 助理详情“会话记录”页签待 T-021；现在是独立页面 `/assistants/:id/conversations`。
- 删除助理会级联删除其会话（外键 `ON DELETE CASCADE`）；助理是软删除，所以实际不会触发，需要在硬删除/租户清除（T-105）时确认。
