# T-005 租户生命周期自动流转与网关同步入队

**阶段**：2　**依赖**：T-002　**PRD**：3.12.6、PLT-053、5.1 租户状态机

## 范围

1. Worker 定时任务（每小时）：
   - 试用到期（`trial_ends_at` 已过）→ 停用；
   - 宽限超过 7 天（以 `status_changed_at` 计）→ 只读；
   - 待删除超过 30 天 → 已删除（**只改状态并撤销会话；真正的数据清除与删除证明属于 T-105，必须在状态详情中明确标注“数据尚未清除”**）。
   自动流转写平台审计与租户审计，操作人为“系统”。
2. 平台创建租户时可选“试用”并设置试用天数（默认 14），写入 `trial_ends_at`。
3. 把 `gateway_sync.py` 的同步调用改为入队执行（`gateway.sync_model`、`gateway.sync_environment`），接口立即返回 `gateway_sync_status="pending"`；前端显示“同步中”，完成后刷新。保留“重试同步”。
4. 只读/停用租户不得产生模型消耗：租户进入只读或停用时，入队任务把该租户所有环境的 LiteLLM 虚拟 Key 置为 blocked；恢复时解除（LiteLLM `/key/block`、`/key/unblock`）。

## 验收标准

- [ ] 用可注入时钟测试三种自动流转与幂等（重复执行不重复写审计）。
- [ ] `scripts/check_gateway_e2e.py` 适配异步同步后仍能通过。
- [ ] 停用租户后，其虚拟 Key 调用网关被拒绝（e2e 脚本增加该检查）。

## 完成记录

**改动文件**
- 后端：`tenant_lifecycle.py`（自动流转 `run_auto_lifecycle`、统一的状态变更 `apply_transition` 与双链审计 `record_lifecycle_audit`，从 `api/platform_tenants.py` 迁入）、`gateway_sync.py`（`request_*_sync` 入队、`gateway.sync_model` / `gateway.sync_environment` / `gateway.sync_tenant_access` 作业、`apply_tenant_access`）、`integrations/litellm.py`（`block_key` / `unblock_key`）、`models/__init__.py`（`gateway_key_blocked`）、迁移 `0026_lifecycle_gateway`（可降级）、`api/tenant_models.py` 与 `api/platform_models.py`（保存/重试同步改为入队，立即返回 `pending`）、`api/platform_tenants.py`（试用创建、`status_note`）、`schemas/__init__.py`、`worker/scheduler.py`（每小时 `tenants.auto_lifecycle`）、`worker/runner.py`、`tests/test_tenant_lifecycle.py`（18 项）；`scripts/check_gateway_e2e.py`、`scripts/check_runtime_e2e.py`（改为等待 Worker，并新增停用租户 Key 被拒绝的检查）。
- 前端：`PlatformTenantsPage.tsx`（开通方式“试用/正式”与试用天数、试用截止、“数据尚未清除”提示）、`TenantModelSettingsPage.tsx` 与 `PlatformModelsPage.tsx`（显示“同步中”并自动刷新直到完成）、`types.ts`、`enums.json`。

**行为**
- 每小时自动流转：试用到期（`trial_ends_at` 已过）→ 停用；宽限超过 7 天（以 `status_changed_at` 计，严格大于）→ 只读；待删除超过 30 天 → 已删除（只改状态并撤销会话）。每次写平台与租户两条审计（操作人为“系统”，无 actor）并通知企业管理员；只匹配仍处于源状态的租户，重复执行不会重复写审计。**已删除租户在平台控制台的状态旁显示“数据尚未清除”**，审计详情含 `data_purged=false`；真正的数据清除与删除证明属 T-105。
- 平台创建租户可选“试用”与试用天数（默认 14，1–90），写入 `trial_ends_at`。
- 网关同步入队：保存模型环境、重试同步、新增/修改/重试平台模型都立即返回 `gateway_sync_status="pending"`，由 Worker 完成并写回 `synced` / `failed`；网关未配置时直接为 `not_configured` 不入队。前端显示“同步中”，每 2 秒刷新到出结果。
- 只读/停用租户不得消耗模型：状态在“可服务（试用/正式/宽限）”与“不可服务（只读/停用/待删除/已删除）”之间变化时，在同一事务入队 `gateway.sync_tenant_access`，作业按**执行时的最新状态**对该租户所有环境的虚拟 Key 调用 LiteLLM `/key/block` 或 `/key/unblock`；网关不可达时作业重试（不会丢）；停用状态下新生成的 Key 一创建即被拦截。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + 全量 291 项）通过；前端检查通过。
- 单元测试覆盖三种自动流转及边界（刚好 7 天不流转）、幂等、下一次运行才会继续推进、经 Worker 执行、试用创建与默认值、入队返回 pending 且请求内不访问网关、网关失败可见并可重试、未配置网关不入队、目录同步、各状态转换是否入队、停用拦截/恢复解除、执行时以最新状态为准、网关故障时重试、停用期新 Key 先拦截。
- **真实栈验证（本机 LiteLLM + Worker，把端点设为公网 IP 以通过 URL 校验，脚本只做访问检查、不联系供应商）**：`scripts/check_gateway_e2e.py` 全部通过——含目录/环境同步由 Worker 完成、停用后租户 Key 被网关拒绝（401）、恢复后重新可用（200）；`scripts/check_runtime_e2e.py` 也全部通过（含 T-024 的发布→回滚、记忆、Langfuse trace）。这两个脚本新增 `E2E_ENDPOINT_URL` 环境变量用于本机 DNS 受限的场景。**更新代码后需要重启 Worker 容器才会加载新作业。**
- **没有在浏览器里目视检查页面。**

**遗留问题**
- 开发栈的 Worker 不会自动热加载，新增作业类型后需 `docker compose restart worker`。
- 自动流转只改状态，不做数据清除（T-105）；“已删除”租户的虚拟 Key 仍只是被拦截，没有从 LiteLLM 删除。
- 网关同步是“尽力一次 + 手动重试”：失败只显示在行上，不会自动重试（与此前一致）；Key 拦截作业有自动退避重试。

**审查修正**
- 修复“保存模型配置会让已正常工作的环境短暂不可用”：同步改为异步后，保存会把环境置为 `pending`，而运行准入、发布前检查、测试调用都要求 `synced`，Worker 处理完之前（Worker 停了则一直）助理无法运行。现统一用 `gateway_sync.environment_ready`：**已有 Key 的环境在 `pending`（重新同步）期间保持可用**；从未同步过（没有 Key）或上次同步 `failed` 的环境仍不可用。已用测试复现后修复。
- 加固“停用租户 Key 拦截”：拦截作业的重试上限从 5 次提高到 30 次（队列退避上限 1 小时，足以覆盖长时间网关故障），并新增每小时一次的对账作业 `gateway.reconcile_access`——找出 Key 状态与租户状态不一致的租户并重新入队，避免重试耗尽后停用租户长期持有可用 Key。
- 自动流转改为在 SQL 中只选出已到期的租户再加行锁，不再每小时锁定全部试用/宽限/待删除租户。
