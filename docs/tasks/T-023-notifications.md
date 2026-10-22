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
