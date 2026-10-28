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
