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
