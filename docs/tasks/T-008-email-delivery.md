# T-008 邮件发送基础设施

**版本**：V0.2（可提前）　**依赖**：T-002　**PRD**：SET-010（邀请邮件）、SET-082（预算告警邮件）、SET-101（邮件通道）、PLT-022（开通邀请）
**外部条件**：生产环境 SMTP / 邮件服务商账号（开发环境用 Mailpit，不需要）

## 目标

把“发邮件”做成统一、可重试、可审计的能力。目前成员邀请只生成链接、不发邮件（development-status 阶段 1 缺口）。

## 设计

- `beework/mail.py`：`send_mail(db, *, tenant_id, to, template, context, locale)` 只入队（T-002 `mail.send`），处理器用 SMTP 发送；失败按队列退避重试，`dead` 后在平台任务页可见。
- 适配器接口 `MailTransport`（`smtp`、`console`）；配置走环境变量 `MAIL_*`，**不写入 `.env` 的真实值**，在 `.env.example` 加占位。
- 开发环境：`compose.yaml` 增加 Mailpit（Web UI 端口只绑定 127.0.0.1），默认 `MAIL_TRANSPORT=smtp` 指向它。
- 模板：`backend/src/beework/mail_templates/{zh-CN,en}/*.txt|html`（Jinja2，自动转义），首批：成员邀请、租户开通邀请、密码重置预留、通用通知。
- `mail_deliveries` 表：收件人（存掩码 + 哈希）、模板、状态、错误、job_id、时间；正文不入库。
- 退订/频率限制：同一收件人同一模板 1 分钟内最多 3 封（防滥用）。

## 范围

1. 上述服务、迁移、Mailpit。
2. 成员邀请（SET-010）与“重新发送”改为真实发信；平台创建租户勾选“立即发送邀请”时发信。
3. 平台后台“邮件投递记录”简单列表（可放进 T-002 的任务页）。

## 验收标准

- [ ] 单元测试：模板中英两种语言渲染、HTML 转义、频率限制。
- [ ] 本地 Mailpit 中能看到邀请邮件，链接可完成接受邀请流程。
- [ ] 日志与审计中不出现完整邮箱以外的敏感信息（邀请 token 不写日志）。

## 不在范围

- 短信、Slack（T-091）；邮件营销。

## 完成记录（2026-11-13，分支 `task/T-008-email-delivery`）

**改动文件**

- 后端新增：`mail.py`（`send_mail` 只入队 `mail.send`、`MailTransport` / `SmtpTransport` / `ConsoleTransport`、Jinja2 渲染、收件人掩码+哈希、频控、worker handler）、`models/mail.py`（`mail_deliveries`）、`schemas/mail.py`、`mail_templates/{zh-CN,en}/`（成员邀请、租户开通邀请、密码重置预留、通用通知，各含 subject/body.txt/body.html）；迁移 `0055_mail_deliveries.py`（down_revision `0054_execution_policy`，可降级）。
- 后端修改：`config.py`（`MAIL_*`）、`models/__init__.py` / `worker/runner.py`（模型与 handler 注册）、`api/jobs.py`（投递记录列表）、`api/members.py`（邀请与重发真实发信）、`api/platform_tenants.py` + `schemas/__init__.py`（`send_admin_invitation`，PLT-022）、`requirements.txt`（jinja2）、`backend/tests/test_mail.py`（35 项）、`backend/tests/test_retire_flow_canvas_migration.py`（既有测试修复，见遗留问题）。
- 前端：`PlatformMailPage.tsx`（邮件投递记录）、`PlatformConsole.tsx`（导航/面包屑）、`PlatformTenantsPage.tsx`（立即发送开通邀请勾选框）、`i18n/{zh-CN,en}/platform.json`。
- 部署：`compose.yaml` 加 Mailpit（Web UI `127.0.0.1:8025`）并给 api/worker 注入 `MAIL_*`；`.env.example` 加占位（无真实密钥）；新增 `scripts/check_mail_e2e.py` 真实栈 e2e。

**接口**：`POST /members/invitations`、`POST /members/invitations/{id}/resend` 除返回链接外在同事务内入队邀请邮件；`POST /platform/tenants` 新增 `send_admin_invitation`（与 `admin_password` 二选一，改为创建 `tenant_admin` 邀请而非预设密码）；`GET /platform/mail/deliveries`（平台管理员，按 status/template 过滤，收件人仅掩码）；`mail.send` job 走 T-002 队列退避重试，`dead` 在平台任务页可见。

**验证命令与结果**

| 命令 | 结果 |
|---|---|
| `docker compose up -d`（`COMPOSE_PROJECT_NAME=beework`，含重建 api/worker 镜像） | 通过；Mailpit healthy，SMTP 由 worker 连通 |
| `(cd backend && uvx ruff@0.14.0 check .)` | 通过 |
| `./scripts/test-postgres.sh` | **1251 项 OK**；含 `alembic upgrade head → check → downgrade base → upgrade head` 与 PostgreSQL 专用测试 |
| `docker compose exec web sh -c "npx tsc -b && npm run lint && npm run build"` | 通过（0 error；8 条既有 React Compiler 警告，均非本次文件） |
| `npm run i18n:check` | 通过（23 个命名空间） |
| `docker compose exec api python /app/scripts/check_mail_e2e.py` | **通过（真实 SMTP）**：平台创建租户勾选立即邀请 → worker 经 SMTP 发到 Mailpit → Mailpit 收到中英文主题与邀请链接 → 平台投递记录 `status=sent`/掩码 → 用邮件链接完成接受邀请、设置姓名密码 → 新管理员可登录 |

**是否连过真实第三方引擎**：邮件链路连过**本地 Mailpit 的真实 SMTP 服务**（非 mock、非假传输）；生产 SMTP / 邮件服务商账号未配置、未连过，部署时需填 `MAIL_*` 并复验。本任务不涉及 LiteLLM / Dify / Langfuse。

**遗留问题**

- 频控按“同收件人 + 同模板 1 分钟 3 封”在应用内计数，无跨实例分布式锁；多 API 实例并发时可能略超。设计条目“退订”未给出对象与页面规则，本次只实现频控，未做退订链接。
- 密码重置、通用通知模板仅预留，没有调用方（密码重置待 T-093；SET-082 预算告警邮件待后续任务接入）。
- 成员邀请仍把 `invitation_url` 一并返回给前端（保持既有兼容），但真实投递已走邮件；平台租户邀请模式下，管理员接受邀请前租户列表的 `admin_email` 为空（尚无成员账号）。
- 前端页面未做浏览器目视验收（与近期任务口径一致）；`check_mail_e2e.py` 在开发库留下 3 个一次性租户与管理员账号。
- 修复了既有测试 `test_retire_flow_canvas_migration.py`：T-160 加入 0052 后，该测试停留在 `0051_retire_flow_canvas` 就跑 `alembic check`，会因“Target database is not up to date”失败；现改为 check 前先 `upgrade head`（不改迁移本身）。

## 补充：重发开通邀请与邮件页 i18n（2026-11-13，分支 `task/T-008b-admin-invitation-resend`）

首版遗留的两项在这里补齐。

- **重发开通邀请**：`POST /platform/tenants/{id}/admin-invitations/{invitation_id}/resend`（平台管理员 + CSRF + 租户行锁）。把待接受或已过期的开通邀请换一个新 token、有效期延长 7 天并重发邮件，旧链接立即失效；首位管理员已加入、邀请已接受或已撤销、租户已删除时 409；一分钟内重发过多返回 429 且不换 token；邮件语言跟随操作的平台管理员；租户审计链与平台审计链各写一条 `tenant.admin_invitation_resent`（邮箱掩码，不含 token）。`GET /platform/tenants` 为每个租户返回最近一次开通邀请的状态（`pending` / `expired`，由 `expires_at` 推出）。迁移 `0056_admin_invitation_name` 给 `member_invitations` 加 `invitee_name`，重发时还原邮件称呼（此前创建的邀请没有称呼，用邮箱前缀代替）。
- **前端**：租户列表“首位企业管理员”一列在管理员尚未接受时显示被邀请人、邀请状态与“重发邀请”按钮。该页其他文案仍是写死的中文，这里保持一致，没有单独国际化。
- **邮件投递页 i18n**：`PlatformMailPage` 的文案移入 `platform` 命名空间的 `mailPage.*`（中英文）。
- **验证**：`test_admin_invitation_resend` 10 项（含 PostgreSQL 并发，需 `TEST_DATABASE_URL`，本次跳过）；与 `test_platform_governance`、`test_tenant_auth`、`test_tenant_lifecycle`、`test_mail`、`test_worker` 合计 153 项通过；ruff、`tsc`、lint（无错误）、`i18n:check`、`style:check` 通过。页面未目视验收；PostgreSQL 并发用例与全量后端未在本次运行。
