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
