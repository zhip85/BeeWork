# T-053 租户术语表

**版本**：V0.2　**依赖**：T-051、T-052　**PRD**：3.10.7（SET-060–063）、OC-035、5.3

## 范围

1. `tenant_terms`（标准术语、别名、定义、`from_id`、`hidden`）与租户继承的行业多选（SET-061，开通时按所选行业预置，T-100 开通流程调用）。
2. 用 T-052 引擎计算生效术语；支持本层隐藏继承术语与“全部恢复”（OC-035）。
3. 页面“设置 → 术语表”：继承链、行业标签切换、术语表（层级、编辑、恢复、删除）、空态、文案与 Toast 按 SET-062。
4. 导入（SET-063）：复用 T-051 解析器。
5. 变更受“助理规则变更”审批策略约束（同 T-052）。
6. 平台术语变更即时反映到未覆盖的租户（读时计算，不复制数据）。

## 验收标准

- [ ] 测试：多行业继承去重规则（同一标准术语出现在两个行业库时，按行业库在租户选择中的顺序取第一个，写进测试）；隐藏与恢复；覆盖后平台修改不影响该术语。

## 完成记录

**改动文件**
- 后端：新增 `governance/term_inheritance.py`（**纯函数**术语继承引擎：行业多选去重 → 租户覆盖 / 隐藏 / 自建 → 助手层，`industry_terms`、`tenant_terms`、`assistant_terms`）、`governance/tenant_glossary.py`（租户层读写、预校验、审批执行器 `tenant.glossary`、平台删除钩子、开通预置）、`models/tenant_glossary.py`（`TenantTerm`、`TenantGlossarySettings`）、迁移 `0037_tenant_glossary.py`、`schemas/tenant_glossary.py`、`api/tenant_glossary.py`、`tests/test_term_inheritance.py`（11 项）、`tests/test_tenant_glossary.py`（22 项）；修改 `glossary.py`（`PLATFORM_TERM_DELETE_HOOKS` / `PLATFORM_GLOSSARY_DELETE_HOOKS`，“租户继承数”改为实际选择该术语库的租户数）、`api/platform_glossary.py`（删除术语 / 术语库时调用钩子）、`api/platform_tenants.py`（新租户按行业预置继承）、`worker/runner.py`、`api/__init__.py`、`tests/test_glossary.py`。
- 前端：共用 `components/glossary/TermImportDialog.tsx`（平台页改用它）、“设置 → 术语表”页 `pages/settings/TenantGlossaryPage.tsx` 与页签、`i18n` 文案。

**迁移**：`0037_tenant_glossary`（两张表，可降级）。`tenant_terms.from_id` 不加外键；`(tenant_id, from_id)` 唯一，保证一个继承术语最多一条覆盖或隐藏记录。

**接口**（`settings:view|edit|delete`）：`GET /tenant/glossary?scope=&q=`（继承链数字、行业标签、生效术语含层级标签与待审批）、`PUT /tenant/glossary/settings`（继承的行业术语库及顺序，带 `row_version`）、`POST /tenant/glossary/terms`、`PUT|DELETE /tenant/glossary/terms/{key}`（编辑继承术语生成 / 更新覆盖；删除自建直接删，删除继承或覆盖则记“隐藏”）、`POST …/{key}/restore`（恢复覆盖）、`POST /tenant/glossary/restore-hidden`（全部恢复）、`POST /tenant/glossary/terms/import`（`dry_run`、`on_duplicate=skip|overwrite`，用 T-051 的解析器）。写操作返回 `applied` 或 202 `pending`。

**设计**
- 继承去重：同一标准术语（不区分大小写与首尾空格）出现在多个所选行业库时，取租户选择顺序靠前的那个；顺序可在页面里前移 / 后移，测试覆盖了顺序翻转。覆盖与隐藏以被继承术语的 id 为键；行业库被取消选择后其覆盖暂时休眠、重新选择后恢复；平台删除术语时，覆盖转为租户自建、隐藏记录清除；删除术语库时从各租户的选择里移除。
- 术语不复制：继承的术语读时计算，平台修改立即反映到未覆盖的租户；已覆盖的不受影响。标准术语在租户当前可见术语中唯一（隐藏的继承术语名可以被自建术语重新使用）。
- 变更走“助理规则变更”审批策略（同 T-052）：提交前预校验，审批对象按术语区分，新增 / 导入 / 继承设置互不阻塞。开通：新租户按其行业预置继承对应的行业术语库（SET-061，T-100 复用 `preset_for_tenant`）。

**验证**：ruff；迁移升降级 + `alembic check`；全量后端 636 项（新增 34 项，更新 T-051 的继承数测试）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看了页面（真实数据，空态）。验收标准：多行业去重与顺序、隐藏与恢复、覆盖后平台修改不影响该术语——均有测试；另测了导入预览 / 跳过 / 覆盖与逐行错误、审批通过 / 驳回、权限（审计员只读、普通查看者无权）、跨租户隔离、平台删除钩子。**没有在浏览器里点过选择行业、编辑、隐藏、导入的完整流程。**

**遗留问题**
- 助手层页面与术语运行时使用（问题改写扩展别名、检索同义词、定义注入）在 T-055 / T-057；引擎已支持助手层（含“继承租户术语表”开关），T-055 需把助手覆盖注册进两个平台删除钩子。
- 已有租户没有继承记录（“未继承”），需要管理员自行选择；开发库里现有租户均为通用办公，没有对应术语库。
- 审批默认开启（安全员一级），开发环境改术语需另一名审批人。
