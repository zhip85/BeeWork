# T-052 继承覆盖引擎与租户规则库

**版本**：V0.2　**依赖**：T-050、T-022　**PRD**：5.3 继承与覆盖、OC-012、3.10.6（SET-050–054）、3.9.1 规则变更

## 目标

做一个 PII 规则与术语共用的“多层继承 + 本层覆盖”引擎，并交付租户层 PII 规则库页面。

## 引擎（`beework/governance/inheritance.py`）

- 每层的行有 `from_id`（覆盖上级哪一条，空表示本层自建）与 `hidden`（术语用：隐藏继承项）。
- `effective_rules(layers, switches)`：严格按 OC-012 的 5 步计算，返回每条规则的层级标签（平台继承 / 租户级 / 租户级 · 已修改 / 助手级 / 助手级 · 已修改）。强制规则的 `enabled` 始终为 true，可改内容。
- 纯函数、无数据库依赖，T-053（术语）与 T-055（助手层）复用；用表驱动单元测试覆盖 OC-012 每一步与开关组合。

## 租户层

- `tenant_pii_rules`（与平台规则字段一致 + `from_id`）与租户开关“继承平台 PII 规则”（SET-051）。
- 页面“设置 → 规则库”（SET-050–052）：继承链两级、开关、筛选（全部生效 / 仅租户级）、编辑继承规则生成覆盖、切换继承规则启用生成覆盖、恢复、删除本层规则；文案与 Toast 按 PRD。
- 前端把规则表做成**一个组件三种模式**（平台 / 租户 / 助手），T-050 平台页与 T-055 助手页改用它。
- 变更受“助理规则变更”审批策略约束（SET-053）：开启时变更进入审批（`submit_change(event="rule_change")`，diff 为规则字段对比），通过后执行器写入；关闭时直接生效。
- 权限：设置 · 规则库（租户管理员 V E D，审计员 V）。写审计。

## 验收标准

- [ ] 引擎单元测试覆盖 OC-012 全部分支（含“PII 检测关闭时只有强制规则生效”）。
- [ ] 测试：编辑平台规则不改变平台表；恢复删除覆盖；审批开启时未通过不生效；跨租户 404。

## 完成记录

**改动文件**
- 后端：`governance.py` 改为包 `governance/`（`__init__.py` 为原 T-050 内容，导入路径不变）；新增 `governance/inheritance.py`（**纯函数**继承覆盖引擎：`Rule`、`Switches`、`effective_rules`、`effective_count`）、`governance/tenant.py`（租户层读写、预校验、审批执行器 `tenant.pii_rules`、平台规则删除钩子）、`models/tenant_rules.py`（`TenantPiiRule`、`TenantPiiSettings`）、迁移 `0036_tenant_pii_rules.py`、`schemas/tenant_pii.py`、`api/tenant_pii_rules.py`、`tests/test_inheritance.py`（15 项）、`tests/test_tenant_pii.py`（16 项）；修改 `worker/runner.py`、`api/__init__.py`、`models/__init__.py`。
- 前端：共用组件 `components/pii/`（`PiiRuleTable`、`PiiRuleDialog`、类型，平台 / 租户 / 助手三种模式）；平台“PII 规则”页改用它；新增“设置 → 规则库”页 `pages/settings/TenantPiiRulesPage.tsx` 与设置页签；`i18n` 文案。

**迁移**：`0036_tenant_pii_rules`（两张表，可降级）。`from_id` 不加外键：删除平台规则时租户覆盖转为租户自建（PLT-133，钩子已接通）。

**接口**（`settings:view|edit|delete`，租户管理员 V E D、审计员 V）：`GET /tenant/pii-rules?scope=all|tenant`（继承链数字、开关、生效规则含层级标签）、`PUT /tenant/pii-rules/settings`（继承平台规则开关，带 `row_version`）、`POST /tenant/pii-rules`（自建）、`PUT /tenant/pii-rules/{key}`（编辑自建或继承规则——继承规则生成 / 更新覆盖）、`POST /tenant/pii-rules/{key}/restore`、`DELETE /tenant/pii-rules/{key}`。写操作返回 `applied` 或 202 `pending`。

**设计**
- 引擎严格按 OC-012 五步：继承开关（关闭只留强制）→ 租户覆盖与自建 → 助手层（引用全局开关）→ PII 检测关闭时只有强制规则生效 → 计数。强制规则 `enabled` 恒为 true、可改内容；平台自己停用的强制规则对所有下级生效（PLT-131）；上级规则不被修改。覆盖的 `key` 始终是平台规则 id，自建规则是自己的 id。
- 租户变更走 `submit_change(event="rule")`（“助理规则变更”策略）：开启时进入审批（diff 为字段对比，同一条规则同一时间只有一个待审批），通过后由执行器写入；关闭时直接生效。提交前先校验名称与正则，避免审批人批准无法执行的变更。规则名称在租户内（含覆盖）及与平台规则之间不得重复（不区分大小写）；一个租户最多 200 条。
- 平台规则的修改即时反映到未覆盖它的租户；已覆盖的不受影响。

**验证**：ruff；迁移升降级 + `alembic check`；全量后端 599 项（新增 31 项）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看了租户“规则库”页与改用共用组件后的平台页（真实数据）。验收标准：引擎单测覆盖 OC-012 全部分支（含检测关闭、三个开关的 6 种组合、强制规则、覆盖与自建）；编辑平台规则不改变平台表、恢复删除覆盖、审批开启时未通过不生效（驳回 / 通过 / 失效规则）、跨租户 404、权限（审计员只读、普通查看者无权）——均已测。**没有在浏览器里点过编辑、恢复、审批的完整流程。**

**遗留问题**
- 助手层页面与存储由 T-055 做；引擎与共用组件已支持助手模式（`assistant` 层、`use_global` / `pii_detection` 开关），平台规则删除钩子目前只转换租户覆盖，T-055 需要把助手覆盖也注册进 `PLATFORM_RULE_DELETE_HOOKS`。
- 审批默认开启（安全员一级），开发环境的租户管理员改规则需要另一名审批人；可在“设置 → 审批策略”关闭。
- 租户层仍只维护 PII 类规则；提示词攻击、内容安全类基线只由平台下发（PRD 8.3 #4）。
- 规则目前只被存储与计算，运行时执行在 T-055。
