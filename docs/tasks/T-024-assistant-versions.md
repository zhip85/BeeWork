# T-024 助理发布、不可变版本、对比与回滚

**阶段**：3　**依赖**：T-021、T-022　**PRD**：AST-044、3.2.8、RUL-001、4.4 AssistantVersion、5.1 助理状态机

## 范围

1. `assistant_versions`：按 PRD 4.4；`snapshot` 是草稿配置的完整副本 + `dsl_version` + 发布时解析出的模型别名与资源版本（知识库映射 ID 等），**不可修改**（ORM 层禁止 UPDATE，迁移加数据库触发器，参考审计表的做法）。
2. “发布更新”：运行配置检查（T-021）→ 有阻断问题则拒绝 → 生成 `change_diff`（与线上版本逐项对比）→ `submit_change(event="publish")`（T-022）。审批通过后的执行器：生成新版本号（vX.Y 递增）、写快照、切换 `live_version_id`、助理状态置为已发布、写审计、通知负责人（`assistant.published`）。
3. 版本页签：时间线、查看快照（只读，含审批记录）、对比任意两个版本（含当前草稿，提示词行级 diff）、“恢复”= 用历史快照覆盖草稿（不直接上线）。
4. 回滚即用历史快照生成新版本，同样走审批。

## 验收标准

- [ ] 发布后继续编辑草稿，线上版本快照不变（测试）。
- [ ] 快照不可更新（PostgreSQL 触发器测试）。
- [ ] 审批关闭时直接发布；审批开启时审批通过才生效。

## T-024a 闭环切片先行范围与完成记录

**范围**：不依赖 T-021（配置检查）和 T-022（审批引擎）的部分——不可变版本与触发器、发布（未配置审批流时直接生效，PRD OC-004 的“未配置则提交即生效”）、版本列表/快照/对比/恢复/回滚、运行时执行线上快照。**不做**（仍属 T-024，待 T-021/T-022）：`submit_change` 审批、完整配置检查与“发布前运行全部测试用例”、`assistant.published` 通知、助理详情页的“版本”页签（现为独立页面 `/assistants/:id/versions`）。审批接入点：`assistant_versions.execute_publish` 就是审批通过后的执行器，T-022 只需在 `publish_assistant` 前拦截并在通过后调用它。

**改动文件**：`models/assistants.py`（`AssistantVersion` + 更新守卫）、`assistant_versions.py`、`schemas/versions.py`、`api/assistant_versions.py`、迁移 `0021_versions.py`（含触发器，可降级）、`tests/test_versions.py`（15 项）、`tests/test_postgres_integration.py`（触发器测试）；修改 `api/runs.py`、`schemas/runs.py`、`models/__init__.py`、`api/__init__.py`、`scripts/check_runtime_e2e.py`；前端 `pages/assistants/{PublishDialog,VersionsPage,VersionDiff}.tsx`、`AssistantsPage.tsx`、`App.tsx`、`i18n/*/versions.json`。

**验证**：`ruff`、`./scripts/test-postgres.sh`（迁移升降级 + `alembic check` + 171 项）、网关 e2e、前端检查通过；真实栈 e2e：发布 v1.0 → 改草稿 → 线上运行执行快照 → 发布 v1.1（含提示词 diff）→ 回滚生成 v1.2 → 数据库拒绝更新已发布版本。测试覆盖：首次发布与状态/草稿版本号、阻断项与预览、无变更拒绝、停用/跨租户/无权限、发布后改草稿不影响线上（含线上与草稿运行读到不同提示词）、ORM 与 PostgreSQL 触发器的不可变、线上运行绑定版本、已发布助理不可删除与复制为草稿、版本列表/详情/隔离、对比（版本对版本、草稿对版本、非法标识）、恢复（乐观锁、名称冲突、不改线上）、回滚（新版本、草稿不变、已是线上、模型不可用、已停用）。仍是 mock 模型 + 检索桩；前端未做浏览器目视验收。

**遗留问题**
- 审批、完整配置检查、测试用例门禁、通知见上；发布后目前没有通知负责人。
- `assistants.live_version_id` 没有外键（`assistants` ↔ `assistant_versions` 循环引用，SQLite 测试不支持后加约束），由代码保证。
- 恢复时若旧名称已被其他助理占用返回 409，由用户先改名；PRD 未规定此情形。
- 版本号规则（1.0 起每次发布/回滚 +0.1，不升主版本）是本任务暂定，PRD 只写“vX.Y 递增”。
- 回滚不校验模型是否曾经可用以外的资源（挂载的知识库等暂未开放，T-033 之后需校验资源版本）。
- 草稿运行在助理已发布且草稿未变时与线上运行结果相同，没有“草稿 = 线上”的提示。
- 快照里保存了完整提示词与配置；日志/导出脱敏策略不适用于版本快照（它是配置不是会话数据）。

## 完成记录（T-024 余下部分：接入配置检查、审批与通知）

**改动文件**
- 后端：新增 `assistant_publish.py`（审批动作 `assistant.publish`）；修改 `api/assistant_versions.py`（发布/回滚改走 `submit_change`，预览新增 `approval`）、`assistant_versions.py`、`approvals.py`（`pending_request`）、`api/assistants.py`（`pending_approval_id`、删除阻断：有待审批单不可删）、`schemas/versions.py`、`schemas/assistants.py`、`worker/runner.py`（worker 注册该动作）、`tests/test_versions.py`（新增 8 项，夹具加第二位管理员并默认关闭发布审批）、`scripts/check_runtime_e2e.py`（检查期间临时关闭发布审批并恢复）。无迁移。
- 前端：`api.ts`（`ApiRequestError` 带 `code`）、`PublishDialog.tsx`、`AssistantsPage.tsx`、`AssistantDetailPage.tsx`、`VersionsPage.tsx`、`types.ts`、`i18n/*`。

**行为**
- `POST /assistants/{id}/publish` 与 `/versions/{vid}/rollback`：先过配置检查（有阻断则 409），再 `submit_change(event="publish", action="assistant.publish")`。策略关闭 → 立即生效，返回 201 + 版本（与此前一致）；策略开启 → 返回 **202** `{pending_approval, approval_id, next_version}`，线上版本与助理状态都不变。回滚与发布共用同一个待审批名额。
- 审批单标题 `发布 {助理} v{版本}` / `回滚 {助理} 到 v{版本}`，带逐项 diff 与自动检查（配置检查的高影响/提示项，全部通过则显示“配置检查通过”）。
- 审批通过后由 worker 执行：**上线的是提交时冻结的快照**（审批人看到的内容），之后改草稿不影响；执行前重新校验（助理已删除/已停用、模型不再启用 → 生效失败并显示原因，可在审批中心重试）。成功后写版本（`approval_id` 关联审批单）、审计、通知助理负责人和申请人（`assistant.published`）。
- APR-002：同一助理已有待审批单时再次提交返回 409 `approval.pending_exists`；带 `replace_pending: true` 则撤回旧单并重新提交。有待审批单的助理不可删除（409）。助理列表/详情显示“审批中”徽标并链接到审批单；版本快照里可跳转审批记录。
- `GET /assistants/{id}/publish-preview` 新增 `approval`（是否需要审批、级数、审批人角色、已有待审批单 ID）。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + 全量 220 项）通过；前端 `tsc -b`、`lint`、`i18n:check`、`build` 通过。
- 新测试：审批开启时发布返回 202、审批前线上不变、通过后 worker 上线并关联审批单/发通知/写审计；上线内容是提交时的快照（之后改草稿不影响）；驳回不发布且可重新提交；重复提交 409、`replace_pending` 撤回旧单、待审批时不可删除；回滚走同一审批；生效失败（模型被停用、助理已停用）可见并重试成功；唯一管理员提交被拒、关闭策略后可直接发布；检查警告进入审批单检查项。原有 15 项直接发布测试在“策略关闭”夹具下保持通过。
- 在运行中的开发栈上冒烟：新字段与预览接口返回正常。**没有在浏览器里目视检查页面**；`scripts/check_runtime_e2e.py` 在本机无法完成——第一步创建模型目录项被 URL 校验拒绝（`api.openai.com` 解析到被判为内网的地址，与本任务无关），因此其中“发布→回滚”这段在真实栈上这次**没有重跑**。

**遗留问题**
- **开发租户只有一位管理员**，默认“发布需审批”开启后，该管理员提交会得到 409 `approval.no_approver`（APR-003）。需要先邀请第二位管理员，或在 设置 → 审批策略 里关闭“发布”。
- “发布前自动运行全部测试用例并把结果附到审批单”（AST-044）依赖 T-027，尚未实现；审批单检查项目前只有配置检查结论。
- 助理详情页的“版本”页签仍是跳转到 `/assistants/:id/versions` 的独立页面，未内嵌。
- 回滚的审批单展示的是“线上版本 → 目标版本”的 diff；审批通过时若线上版本已被别的发布替换，仍按提交时的快照执行（版本号在执行时才确定）。
- 租户只读期间 worker 仍会执行已通过的审批（T-022 遗留，待产品规则）。

**审查修正**
- 修复过期审批单覆盖新版本：审批开启期间已有待审批单，之后策略被关闭并直接发布新内容，旧审批单仍可被批准，批准后会用旧快照把线上版本覆盖回去。现在策略关闭、变更直接生效时，同一对象上的待审批单会被自动撤回（审计 `approval.withdrawn`），并通知原申请人（通知类型 `approval.withdrawn`，中英文文案已补）。测试覆盖：旧单被撤回、再批准返回 409、线上保持新内容。
- 已知但未改：任何对该助理有发布权限的人，用 `replace_pending` 提交时都能撤回他人的待审批单（APR-002 只写了“是否撤回并重新提交”，没有限定申请人）；列表接口为每个助理多查一次待审批单（与现有逐项查询方式一致，每页 ≤100 条）。
