# T-022 审批策略、审批引擎与审批中心

**阶段**：3　**依赖**：T-001　**PRD**：3.9（APR-001–025）、3.10.8 审批策略（SET-071）、4.11、PER-001、5.1 审批单状态机、8.3#7、#16

## 目标

做一个通用审批引擎：任何“受控变更”都能提交审批，策略决定是否需要审批、谁审、几级；审批通过后由系统执行变更。T-024 发布助理是第一个使用方。

## 数据模型

- `approval_policies`：每租户每事件一行（SET-071 表格的 7 个事件及默认值），开通租户时写入默认值（迁移为现有租户补齐）。
- `approval_requests`：按 PRD 4.11；`levels` 记录每级审批人角色、可审批人快照、状态、决定人、意见、时间；状态：pending/approved/rejected/withdrawn/expired，另加 `apply_status`（pending/succeeded/failed）与 `apply_error`。
- `approval_comments`。

## 引擎

- `submit_change(db, *, tenant_id, event, object_type, object_id, title, change_diff, checks, requester)`：策略关闭 → 直接执行；否则建审批单。同一对象同一类型只能有一个待审批单（APR-002）。
- 动作执行器注册表：`@approval_action("assistant.publish")`，审批全部通过后**入队**执行（T-002），执行失败记 `apply_status=failed` 可重试（APR-023）。
- 审批人解析：租户管理员 = 角色为租户管理员的成员；安全负责人/知识负责人 = 自定义角色上的“负责人标记”（在 `tenant_roles` 加 `approver_tags` 字段，角色编辑器可勾选）；助理负责人 = 对象的 `owner_id`。二级审批第 2 级固定为租户管理员。
- 申请人不能审批自己；可审批人只有申请人时升级到租户主管理员（APR-003）。
- 两级审批仅企业版可用（权益 `approval_levels`，来自 T-010）。
- 定时任务：7 天过期、24 小时催办（APR-024），依赖 T-002。
- 审批状态变化写审计、发站内通知（T-023，未完成时留钩子）。

## 接口与界面

- `GET/PUT /tenant/approval-policies`（设置 → 合规与审批 → 审批策略，SET-071，企业管理员）。
- `GET /approvals?scope=pending_mine|submitted|all&status=&type=`、`GET /approvals/{id}`、`POST /approvals/{id}/approve|reject|withdraw|comment`。
- 审批中心页（APR-010–013）与审阅抽屉（APR-020–021）；侧栏审批角标（GLB-006）。

## 验收标准

- [ ] 状态机与多级流转有完整测试，含并发下同一审批单不被重复通过（PostgreSQL 测试）。
- [ ] 一个测试用动作执行器验证：通过 → 入队 → 执行成功；执行失败 → 可重试。
- [ ] 审计员可查看全部审批（只读），构建者只能看自己提交的和待自己审批的（6.1）。

## 完成记录

**改动文件**
- 后端：新增 `models/approvals.py`（`ApprovalPolicy`、`ApprovalRequest`、`ApprovalComment`）、`approvals.py`（引擎）、`schemas/approvals.py`、`api/approvals.py`、迁移 `0022_approvals`（可降级；另给 `tenant_roles` 增加 `approver_tags`）、`tests/test_approvals.py`（22 项）；修改 `models/__init__.py`（注册、`approver_tags`、审计动作描述）、`api/roles.py` 与 `schemas/__init__.py`（角色的审批负责人标记）、`api/__init__.py`、`worker/scheduler.py`（每 5 分钟入队 `approvals.sweep`）、`worker/runner.py`（注册作业）、`tests/test_worker.py`（计划任务清单）、`tests/test_postgres_integration.py`（2 项并发测试）。
- 前端：新增 `pages/approvals/ApprovalsPage.tsx`（审批中心 + 审阅抽屉）、`pages/settings/ApprovalPoliciesPage.tsx`（SET-071）、`i18n/{zh-CN,en}/approvals.json`；修改 `App.tsx`（侧栏“审批中心”、待办角标每 60 秒刷新、路由 `/approvals`、`/settings/approvals`）、`SettingsTabs.tsx`、`RoleManagementPanel.tsx`（“审批负责人”勾选）、`types.ts`、`App.css`、`i18n/index.ts`、`nav.json`、`settings.json`。

**引擎用法**
- 来源操作调用 `approvals.submit_change(db, tenant_id=, event=, action=, object_type=, object_id=, title=, payload=, change_diff=, checks=, requester_id=, owner_user_id=, replace_pending=)`；返回 `SubmitResult(approval, applied_directly)`。策略关闭则立即执行已注册动作；开启则建审批单。动作用 `@approval_action("assistant.publish")` 注册，签名 `fn(db, ApplyContext)`。
- 全部级别通过 → 状态「已通过」，入队 `approvals.apply`（幂等键含重试次数）；执行失败记 `apply_status=failed` 与原因，`POST /approvals/{id}/retry` 重新入队。取值为 pending / succeeded / failed（PRD 写 applied，这里沿用任务规格）。
- 同一对象同一类型的待审批单由数据库部分唯一索引保证（含并发）；`replace_pending=True` 撤回旧单再提交（APR-002）。
- 通知钩子 `approvals.NOTIFIERS`（T-023 注册）；审批状态变化均写审计（`approval.*`，归入“安全”类）。

**接口**：`GET/PUT /tenant/approval-policies`（读 `settings:view`，写仅租户管理员；二级审批需权益 `approval_levels=multi`，否则 403 `plan.feature_locked`）、`GET /approvals`（`scope/status/type/organization_id/q/sort`）、`GET /approvals/summary`、`GET /approvals/{id}`、`POST /approvals/{id}/approve|reject|withdraw|comment|retry`。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + 全量 203 项）通过；前端 `tsc -b`、`lint`、`i18n:check`、`build` 通过。
- 覆盖：默认策略与校验/审计/403/409 冲突；套餐权益；策略关闭直接生效；审批人解析（排除申请人、安全负责人标记、回退管理员、唯一管理员被拒）；单级与二级流转、驳回必填意见、撤回、非审批人 403/不可见 404；生效入队执行一次、失败可见并重试、未注册动作失败可见；24 小时催办一次与 7 天过期；可见性矩阵（管理员/审计员全部、构建者仅自己和待己、查看者无）；跨租户 404、未登录 401。PostgreSQL 并发：4 人同时审批同一单只有 1 人成功且只入队 1 个生效作业；4 次并发提交同一对象只留 1 个待审批单。
- 在运行中的开发栈上执行迁移并冒烟：登录 → 策略默认值 → 汇总接口。**没有在浏览器里目视检查页面**，界面仅由 `tsc`/lint/build 保证，验收时请手动点一遍审批中心、审阅抽屉、审批策略页、角色编辑器的“审批负责人”和侧栏角标。未连接任何第三方引擎。

**遗留问题 / 请开发负责人确认**
- 还没有真实的来源操作：助理发布仍直接生效，T-024 需改为 `submit_change`（动作 `assistant.publish`）。**开发租户只有一位租户管理员，默认“发布需审批”开启后，该管理员自己提交会得到 409 `approval.no_approver`**（APR-003 的自审限制）；T-024 接入前请先邀请第二位管理员，或在策略里关闭“发布”。PRD 对“申请人是唯一可审批人”只写了升级到租户主管理员，没有写主管理员本人就是申请人的情形，这里按拒绝提交处理，请确认。
- 策略行采用“首次读取/提交时按 PRD 默认值懒创建”，没有在开通租户时写入、也没有迁移回填；效果相同，且新增事件类型时无需再回填。
- 审批单列表的可见性与“待我审批”过滤在应用层完成（审批人快照存于 JSON），按租户内全部审批单加载后分页；试点规模可接受，量大时再加索引列。
- 审批中心“本月已完成”卡片目前只切换到“全部申请”范围，可再用状态筛选查看；“全部组织”筛选 UI 未做（接口已支持 `organization_id`）。
- 助理负责人取对象的 `owner_user_id`（由来源操作传入）；`approvals:approve` 权限位未使用，审批权限只来自策略（PER-001）。
- `approval.updated` Webhook 属 T-087，站内通知属 T-023，目前只留钩子。

**验收修正（开发负责人审查后）**
- 修复二级审批的套餐限制可绕过：此前可在策略“关闭”时保存 2 级，再开启；现在套餐不含多级审批时，任何把级数改为 2 的保存都返回 403（已有的 2 级保持不变）。
- 对齐 PER-001（审批权限与模块权限独立）：审批接口（列表、汇总、详情、通过/驳回/撤回/评论/重试）改为任意在职成员可调用，可见与可操作范围仍由“是否为申请人 / 审批人 / 管理员 / 审计员”限定；`approvals:view` 现在只影响无任何审批关联的成员是否看到侧栏入口。`GET /approvals/summary` 新增 `visible_count`，前端据此为审批人显示入口。
- 仍保留：只读租户期间 worker 仍会执行已通过的审批（待产品规则）；审批中心列表前端只取前 100 条。
