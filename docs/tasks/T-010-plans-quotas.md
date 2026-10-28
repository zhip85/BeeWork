# T-010 套餐版本、权益、订阅快照与配额服务

**阶段**：2　**依赖**：T-001　**PRD**：3.14.1（PLT-080–085）、RUL-021、5.6、SET-011、SET-080

## 目标

把套餐从租户表上的一个字符串，变成有版本的套餐定义 + 租户订阅快照 + 统一的配额/权益判定服务，供后续所有模块调用。

## 数据模型

- `plans`：`code`（standard/professional/enterprise）、`name`、`description`、`price_text`。
- `plan_versions`：`plan_id`、`version`、`entitlements` JSON（PLT-081 每一行，如 `{"mcp": false, "environments": "production_only", "approval_levels": 1, "log_retention_days": 30, ...}`）、`quotas` JSON（PLT-082：`assistants`、`seats`、`monthly_points`、`storage_gb`、`monthly_conversations`、`api_qps`，`null` 表示不限）、`overage_policy` JSON（每项 hard/soft/degrade）、`published_at`。
- `tenant_subscriptions`：`tenant_id`、`plan_version_id`、`entitlements`/`quotas` **快照**、`billing_cycle`（monthly/yearly）、`renews_at`、`addons` JSON、`created_at`；每个租户只有一条生效订阅（历史保留）。
- 迁移：建表并写入三套餐 v1 种子数据；为每个现有租户按 `tenants.plan` 生成订阅快照；`tenants.seat_limit` 改为从订阅读取（保留列做兼容，或迁移后删除，二选一并在迁移说明中写清）。

## 服务

`beework/entitlements.py`：

- `get_subscription(db, tenant_id)`、`has_entitlement(db, tenant_id, key)`、`require_entitlement(key)` 依赖（未开通返回 403，`code="plan.feature_locked"`，文案“当前套餐未包含，请联系平台升级”）。
- `check_quota(db, tenant_id, key, increment=1)`：返回 `QuotaDecision(allowed, used, limit, policy)`；硬限制时调用方返回 409 `code="plan.quota_exceeded"`。用量来源：助理数、席位由业务表计数；点数、会话数由 T-011 的用量表计；QPS 由 T-028 限流器执行。
- 替换现有席位逻辑（成员邀请/恢复）与 `environments_for_plan` 为订阅驱动。

## 接口与界面

- 平台：`GET /platform/plans`（含各版本、使用租户数）；`POST /platform/plans/{code}/versions` 新建版本（PLT-085）；`POST /platform/tenants/{id}/subscription` 应用订阅变更（写审计）。平台后台“套餐与权益”页：套餐卡 + 权益表 + 配额表（PLT-080–082），可编辑并发布新版本。
- 租户：`GET /tenant/subscription` 返回套餐、权益、配额与当前用量；“设置 → 订阅与用量”页先展示当前套餐卡与配额用量（SET-080 中点数部分等 T-011）。

## 验收标准

- [ ] 平台发布新套餐版本后，已有租户仍保持旧快照，直到应用订阅变更。
- [ ] 不允许删除有租户使用的套餐（RUL-013）。
- [ ] 测试：权益锁定 403、席位硬限制、套餐降级后已有对象保留但不可新增。

## T-010a 闭环切片先行范围（优先线，见看板“闭环切片”）

只做让 T-020 能创建助理所需的最薄一层，其余仍属本任务（T-010 剩余部分）：

- **做**：`plans`/`plan_versions`/`tenant_subscriptions` 三张表与迁移（三套餐 v1 种子、现有租户生成订阅快照）；`beework/entitlements.py` 的 `get_subscription`、`has_entitlement`、`require_entitlement`、`check_quota`（至少支持 `assistants`、`seats`）；席位逻辑改为订阅驱动；`GET /tenant/subscription`（只读）。
- **不做（留给 T-010 剩余部分）**：平台“套餐与权益”页、`GET /platform/plans`、新建版本、`POST /platform/tenants/{id}/subscription`、租户“订阅与用量”页、RUL-013 删除限制。
- 测试：权益锁定 403、席位/助理数硬限制 409（`plan.quota_exceeded`）。
- 完成 T-010a 后，T-020 的依赖即满足；本任务整体仍保持「待开发」直到剩余部分完成。
