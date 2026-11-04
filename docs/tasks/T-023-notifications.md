# T-023 站内通知中心

**阶段**：3　**依赖**：T-001　**PRD**：GLB-025、SET-102、5.8（站内列）、4.12 InAppNotification

## 范围

1. `in_app_notifications` 表与服务 `notify(db, *, tenant_id, recipient_ids|role, type, title, body, link)`，同事务写入。
2. 接口：`GET /notifications?unread=`（分页）、`GET /notifications/unread-count`、`POST /notifications/{id}/read`、`POST /notifications/read-all`。
3. 顶栏铃铛：未读红点、面板（全部 / 未读 N）、点击标记已读并跳转、“查看全部通知”列表页。轮询间隔 60 秒（SSE 推送后续再做）。
4. 接入已有事件：成员邀请被接受（通知邀请人）、租户被平台变更状态（通知企业管理员）。审批、评测、预算告警由各自任务调用 `notify`。

## 验收标准

- [ ] 只能读到自己的通知（跨用户、跨租户测试）。
- [ ] 通知 link 只能是站内相对路径。

## 完成记录

**改动文件**
- 后端：新增 `models/notifications.py`（`InAppNotification`）、`notifications.py`（`notify`、`safe_link`、审批通知器）、`schemas/notifications.py`、`api/notifications.py`、迁移 `0023_notifications`（可降级）、`tests/test_notifications.py`（6 项）；修改 `models/__init__.py`、`api/__init__.py`、`worker/runner.py`（worker 进程也注册审批通知器）、`api/members.py`（邀请被接受 → 通知邀请人）、`api/platform_tenants.py`（租户状态变更 → 通知该租户的企业管理员）、`tests/test_tenant_auth.py`（邀请接受后收到通知的断言）。
- 前端：新增 `components/NotificationBell.tsx`（铃铛、面板、60 秒轮询）、`components/notificationText.ts`、`pages/notifications/NotificationsPage.tsx`、`i18n/{zh-CN,en}/notifications.json`；修改 `App.tsx`（铃铛、`/notifications` 路由与页面标题）、`ApprovalsPage.tsx`（`?id=` 直接打开审阅抽屉）、`App.css`、`i18n/index.ts`。

**接口**：`GET /notifications?unread=&limit=&offset=`、`GET /notifications/unread-count`、`POST /notifications/{id}/read`（幂等）、`POST /notifications/read-all`。均只作用于当前用户在当前租户的通知，他人的通知返回 404；写接口需 CSRF。

**服务**：`notify(db, *, tenant_id, type, title, body, link, params, recipient_ids, role)` 在调用方事务内写入，同一用户只一行；`link` 只允许站内相对路径（拒绝 `https://`、`//`、`javascript:`、空白与反斜杠），否则抛 `ValueError`。通知同时存 `type` + `params`，前端对已知类型按当前语言渲染，未知类型回退到服务端写入的中文文本。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + 全量 210 项）通过；前端 `tsc -b`、`lint`、`i18n:check`、`build` 通过。
- 覆盖：只能读到自己的通知（跨用户、跨租户 404/403、未登录 401、read-all 不碰他人）；链接白名单；角色/ID 定向与去重；未读过滤、排序、分页、幂等已读、缺 CSRF 403；审批提交通知审批人、通过后通知申请人；租户状态变更只通知企业管理员且状态不变不重复通知；邀请被接受通知邀请人。
- 在运行中的开发栈上执行迁移并冒烟：写入一条通知 → 登录会话读取未读数 → 标记已读 → 未读数归零（冒烟数据已清理）。**没有在浏览器里目视检查页面**，界面仅由 `tsc`/lint/build 保证，验收时请手动点一遍铃铛面板、标记已读并跳转、“查看全部通知”页，以及点审批通知后是否直接打开审阅抽屉。未连接任何第三方引擎。

**遗留问题**
- 评测完成、预算告警、渠道异常等通知由各自任务调用 `notify`；本任务只接入审批、成员加入、租户状态变更。
- 租户状态由自动流转（T-005）变化时尚未发通知（当前只有平台管理员手动操作会触发）；T-005 完成后应在同一处调用。
- 轮询为 60 秒，不含 SSE 推送；标题与正文的中文回退文本存库，新增通知类型时需同时补前端 `types.*` 文案。
- 通知没有保留期清理；量大后需要按已读且超过一定天数的记录做定期清理。

**审查修正**
- 修复只读租户无法标记通知已读：租户变为只读/暂停时发出的“状态已变更”通知，原先会因为统一的只读写拦截而无法标记已读（403）。新增依赖 `auth.get_tenant_membership_for_own_state`，仅用于“标记已读 / 全部已读”这两个只改自己通知状态的接口；其他写操作在只读租户中仍被拦截（测试覆盖 `read_only` 与 `suspended`）。
