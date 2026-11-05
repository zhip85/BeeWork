# T-011 用量汇总：LiteLLM 花费 → 点数

**阶段**：2　**依赖**：T-002、T-010　**PRD**：RUL-020、RUL-022、SET-080、SET-081、SET-082、4.12 UsageRecord

## 设计

- 计量事实来自 LiteLLM：每次调用的 token 与花费按 team（= 租户 × 环境）记录在 LiteLLM 的 spend logs。BeeWork 不自己统计 token。
- Worker 每 5 分钟拉取增量（LiteLLM `/spend/logs` 或 `/global/spend/report` 等接口，按 `start_date` 增量 + 去重键），换算点数：`tokens_in/1000 × 模型输入扣点 + tokens_out/1000 × 模型输出扣点`（扣点取平台模型目录），写入 `usage_records`（日 × 租户 × 环境 × 模型 × 助理 × 来源）。请求 metadata 中携带的 `assistant_id`、`run_id`、`source`（live/debug/test/eval）由 T-025 在调用时传入 LiteLLM。
- **先确认接口**：实现前用本地 LiteLLM 实测 spend 相关接口的字段与分页，把结论写进本文件“实测记录”一节；若需要开启 LiteLLM 的 spend 记录配置，同步修改 `config/litellm/config.yaml`。
- 预算与告警（SET-082/083）：租户月度预算、阈值、接收人；跨阈值每月只通知一次（站内通知，依赖 T-023 时可后补）；开启“100% 暂停非生产流量”时，把开发/预发环境的虚拟 Key 置为 blocked，月初恢复。

## 界面

- “设置 → 订阅与用量”：本月已用点数 / 套餐点数、席位、预计月末用量（日均线性外推）、最近 6 个月柱状图 + 预算线、按助理拆分、导出 CSV（日 × 助理 × 模型 × 环境）。

## 验收标准

- [ ] 拉取任务幂等（重复拉取同一时间窗不重复计数），有测试（LiteLLM 响应用 MockTransport）。
- [ ] 本地用 LiteLLM 真实记录验证一次（无供应商 Key 时可在 LiteLLM 中注册一个 `mock_response` 的 deployment 产生 spend 记录），在 development-status 中记录结果。

## 实测记录（本地 LiteLLM，2026-11-05）

- **接口选择**：`GET /spend/logs` 不带 `request_id` 时只返回按日汇总，不能用；逐条记录用 **`GET /spend/logs/v2`**，参数 `start_date` / `end_date`（`YYYY-MM-DD HH:MM:SS`，UTC）、`page`、`page_size`、`sort_by=startTime`、`sort_order=asc`，响应 `{data, total, page, page_size, total_pages}`。
- **每条记录可用字段**：`request_id`（去重键）、`startTime`、`prompt_tokens`、`completion_tokens`、`spend`（美元）、`model_id`（= BeeWork 注册部署时传入的平台模型 ID，所以能直接对到目录里的扣点单价）、`status`（`success` / `failure`）、`metadata.user_api_key_alias`（= `beework:<租户>:<环境>`，由 BeeWork 创建 Key 时设置，调用方无法伪造）、`team_id`。失败的调用没有 `model_id`、token 为 0。
- **重要发现**：请求体里的 `metadata`（`assistant_id`、`run_id`、`source` 等）**不会**被 LiteLLM 写进花费记录，只有放在 `metadata.spend_logs_metadata` 下的才会（实测：原样传 `metadata` 时 `requester_metadata`/`spend_logs_metadata` 都为空；放进 `spend_logs_metadata` 后记录里能读回）。所以 `runtime/llm.py` 与 `integrations/litellm.py` 现在把调用元数据同时放进 `spend_logs_metadata`。T-025 之前的历史记录没有助理归属，会计入“未归属”。
- **写入有延迟**：花费记录由 LiteLLM 批量异步落库（实测约 10 秒内可读），所以每次拉取都重读游标前 30 分钟，靠 `usage_seen`（请求 ID 去重）保证不重复计数。
- **不需要改 `config/litellm/config.yaml`**：`database_url` 已配置，spend 记录默认开启。

## 完成记录（T-011）

**改动文件**
- 后端：新增 `models/usage.py`（`usage_records`、`usage_seen`、`usage_sync_state`、`tenant_budgets`、`usage_alerts`）、迁移 `0028_usage_metering`（可降级）、`usage.py`（拉取、点数换算、预算与告警、暂停、执行点检查）、`schemas/usage.py`、`api/usage.py`、`tests/test_usage.py`（15 项）、PostgreSQL 并发拉取测试、`scripts/check_usage_e2e.py`；修改 `integrations/litellm.py`（`spend_logs`）、`runtime/llm.py`、`gateway_sync.py`（预算暂停并入密钥封禁判断）、`entitlements.py`（点数、会话数计数器）、`conversations.py`、`channel_chat.py`、`api/runs.py`、`api/test_cases.py`、`worker/scheduler.py`、`worker/runner.py`。
- 前端：`pages/settings/UsagePage.tsx`（设置 → 订阅与用量）、`SettingsTabs.tsx`、`App.tsx`、`i18n/*/settings.json`、`i18n/*/notifications.json`（`usage.threshold`）、`App.css`。

**行为**
- Worker 作业 `usage.sync` 每 5 分钟拉取增量：按 `user_api_key_alias` 归属租户与环境，按 `spend_logs_metadata` 归属助理与来源（live/debug/test/eval），按平台模型目录换算点数 = 输入 token/1000 × 输入扣点 + 输出 token/1000 × 输出扣点，累加进按（租户、日、环境、模型、助理、来源）的 `usage_records`。幂等：请求 ID 先写 `usage_seen` 再累加，同一事务；PostgreSQL 上用 advisory lock 串行化并发拉取。失败/零 token 调用只做标记；找不到租户的调用（如平台自己的模型测试）记为未归属并跳过；目录里已不存在的模型按 0 点计并计数。
- 月度点数（UTC 自然月）= 当月 `usage_records` 求和向上取整，注册为 `monthly_points` 配额计数器：控制台运行、测试批次、开放 API / 挂件在**开始新运行前**检查，硬限制返回 409 `plan.quota_exceeded`（开放 API 为 402 `quota_exceeded`）。`monthly_conversations` 计数器统计本月非调试/测试的会话，开始新会话时检查。
- 预算（软上限，仅告警）：默认 50,000 点、阈值 80/100%、接收人、“100% 暂停非生产流量”（默认关）。每个阈值每月只触发一次（`usage_alerts` 唯一键），向租户管理员发站内通知 `usage.threshold`。开启暂停且达到 100% 时：开发/预发环境新运行被拒绝（403 `usage.environment_paused`），并入队 `gateway.sync_tenant_access` 封禁这两个环境的虚拟 Key（重试 30 次；每小时 reconcile 兜底）；月初 `usage.month_rollover` 或调高预算/关闭开关后解除并取消封禁。
- 接口：`GET /tenant/usage`（本月点数/套餐点数、席位、预计月末、近 6 个月、本月按助理前 3 + 其他、预算、最近汇总时间/错误）、`PUT /tenant/usage/budget`、`GET /tenant/usage/export?start&end`（CSV：日 × 助理 × 模型 × 环境，UTF-8 BOM，公式前缀转义，范围 ≤ 366 天）。

**验证**
- 见 `docs/development-status.md` 的验收记录（测试数与真实栈端到端结果）。
- **真实栈端到端**（`scripts/check_usage_e2e.py`，本机 LiteLLM + Worker，部署改为 `mock_response`）：真实运行一次后，拉取到的花费记录被归属到正确的租户/环境/助理/来源/模型，token 取自网关，点数等于按目录单价的换算值（22 个输入 + 11 个输出 token → 0.044 点），再次拉取不重复计数，API 本月合计与 CSV 导出都包含这次调用。**没有真实供应商 Key，所以 token 数是 mock 的，不是真实模型的。**

**遗留问题 / 需产品确认**
- 邮件告警未接入（项目里没有邮件发送能力）：接收人只保存；`usage.threshold` Webhook 属 T-087。
- “超额策略”中的软限制（超出计费）与降级（切换降级模型）尚未实现，目前只有硬限制真正生效；PLT-084 的“已用量达告警阈值时通知平台”只做了租户预算告警，平台侧通知待 T-102/T-103。
- RUL-020 的“自有密钥不扣点”依赖 T-012（BYOK）；“额外 100 万点数”加购叠加月度上限依赖 T-102（订阅 `addons` 字段已预留，未参与计算）。
- 点数月份按 UTC 自然月；若产品要按租户时区或订阅周期起算需要确认。
- 首次部署时只从当月 1 日起拉取，更早月份的柱状图为空（无回填）；T-025 之前的记录没有助理归属。
- 点数检查发生在运行开始前，用量汇总有最长约 5 分钟的延迟，并发在途的运行可能让当月略微超出上限。
- 未做浏览器目视验收（页面仅通过类型检查、构建和 i18n 检查）。



**审查修正（T-011）**
- 修复默认告警不生效：预算行此前只在租户打开“订阅与用量”页时才创建，从没打开过的租户即使超过默认预算的 80%/100% 也不会收到任何告警（也不会触发暂停）。现在评估预算时会按默认值创建，已用测试复现并覆盖。
