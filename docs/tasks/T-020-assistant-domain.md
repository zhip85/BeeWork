# T-020 助理领域模型、列表与创建向导

**阶段**：3　**依赖**：T-001、T-010a（T-010 的闭环切片先行部分）　**PRD**：3.2.1（AST-001–010）、3.2.2（AST-020–024、AST-030）、4.4、5.1 助理状态机、5.7

## 数据模型（`models/assistants.py`）

`assistants`：按 PRD 4.4 字段；另加 `row_version`、`created_by`、`updated_by`、`deleted_at`。草稿配置放在 `draft_config` JSON（`prompt`、`model_id`、`driven_by`、`default_language`、`greeting`、`suggested_questions`、`feedback_enabled`、`mounts`，以及 OC 等后续字段的占位），`draft_version`（如 `0.1`），`live_version_id`（T-024 使用，先可空）。`draft_config` 的结构用 Pydantic 定义并带 `dsl_version: 1`，T-025 编译器按它读取。

## 权限

- 新权限已在 `rbac.PERMISSION_CATALOG` 中（`assistants:view/edit/publish/delete/test`）；接口按 PRD 6.1 使用。
- 组织范围：列表只返回用户有权组织内的助理（T-001 `scope_filter`）；可见范围 `visibility` 的语义写清并测试。
- **助理范围角色**（SET-022“助理”范围）：在 `TenantRole.scope` 增加 `assistant` 与 `scope_assistant_ids`，授权时校验。

## 接口

- `GET /assistants`（搜索、状态、组织多选、分页、排序 AST-009）、`POST /assistants`（向导创建或保存草稿）、`GET /assistants/{id}`、`PUT /assistants/{id}`（编辑草稿，`row_version`）、`POST /assistants/{id}/copy`、`POST /assistants/{id}/disable|enable`、`DELETE /assistants/{id}`（仅草稿/已停用；有渠道绑定时 409）。
- 统计：`GET /assistants/summary`（总数、已发布、草稿、套餐用量）；月会话量在 T-026 后补，先返回 null。
- 创建前 `check_quota("assistants")`（AST-024）。
- 模型只能选租户**当前环境已启用**的对话模型；默认取环境默认对话模型（5.3 模型继承）。

## 界面

- 助理列表页（卡片/列表视图、筛选、统计条、更多菜单）与创建/编辑向导（4 步）。第 3 步“能力”选择器先只支持知识库类型的占位（T-033 补齐），其余类型显示“即将开放”。模板市场入口显示“即将开放”（T-111）。
- 侧栏“助理”菜单接入真实页面，替换占位。

## 验收标准

- [ ] 名称租户内唯一（不区分大小写）、长度校验、提示词 ≤ 8000。
- [ ] 跨租户、跨组织访问返回 404；查看者不能创建（403）。
- [ ] 删除限制、停用/启用、复制规则符合 AST-010。
- [ ] 所有写操作写审计（分类“助手”：在 `AuditEvent` 分类规则中把 `assistant.` 前缀映射到 `assistants`）。

## 完成记录

**改动文件**
- 后端：新增 `models/assistants.py`、`models/mixins.py`（通用列 mixin 移出 `api/conventions.py` 以避免模型→API 循环导入，`conventions` 仍重新导出）、`schemas/assistants.py`、`api/assistants.py`、迁移 `0016_assistants.py`（可降级）、`tests/test_assistants.py`（15 项）；修改 `models/__init__.py`（注册、审计分类 `assistant.*` → `assistants`、动作描述、`TenantRole.scope_assistant_ids`）、`rbac.py`（`ASSISTANT_SCOPE_MODULES` 与权限依赖拦截）、`api/roles.py` 与 `schemas/__init__.py`（助理范围）、`api/__init__.py`。
- 前端：新增 `pages/assistants/`（`AssistantsPage`、`AssistantWizard`、`types`）、`components/ConfirmDialog.tsx`、`i18n/*/assistants.json`；修改 `App.tsx`（路由、去掉写死的“12”）、`RoleManagementPanel.tsx`、`types.ts`、`i18n/index.ts`、`App.css`。

**迁移 / 接口**：见 development-status 本节；revision `0016_assistants`。

**验证**
- `ruff` 通过；`./scripts/test-postgres.sh`（迁移升降级 + `alembic check` + 87 项）通过；前端 `tsc -b`、`lint`、`i18n:check`、`build` 通过。
- 测试覆盖：创建成功与默认值、审计分类、校验失败（名称空/超长、简介/提示词空、提示词超长、模型未启用、非空挂载/驱动、可见范围、分类）、名称不区分大小写唯一与删除后释放、草稿保存放宽与“未命名助理”、查看者创建/删除/复制 403、跨租户 get/delete/copy/disable 404 与跨租户组织 404、配额硬限制（创建与复制 409 `plan.quota_exceeded`）、`row_version` 冲突、仅管理员可改组织、复制规则、状态机与删除阻断注册点、列表筛选/搜索/排序/分页、组织范围角色的可见与写入范围、助理范围角色的校验与端到端、删除后从角色范围移除。
- 在运行中的开发栈上对真实 PostgreSQL 做了冒烟：登录 → 汇总 → 保存空草稿（得到“未命名助理”）→ 完整创建（因开发租户尚未启用对话模型，按预期返回 `assistant.no_chat_model`）→ 删除。**没有在浏览器里目视检查页面**（无浏览器工具），界面仅由 `tsc`/lint/build 保证，验收时请手动点一遍列表、向导和角色编辑器。未连接任何第三方引擎。

**遗留问题**
- 开发租户需先在“设置 → 模型与环境”启用对话模型，才能完整创建助理。
- 模型选项固定为 `production` 环境；T-006 后改为随当前环境。
- 以下 AST-010 菜单项、卡片指标、模板、驱动方式、能力挂载与“AI 生成建议”依赖后续任务（T-021/T-024/T-026/T-027/T-029/T-040/T-111 等），目前显示“即将开放”或省略。
- 可见范围的写入规则是本任务的定义（可见 ≠ 可改）：PRD 未写明，请开发负责人确认。
- 助理范围角色的权限被限制为工作台与助理两个模块（PRD 只说“只对指定助理及其渠道、会话生效”，渠道/会话权限待相应任务扩展）。
- 旧 `ensure_unique_name` 不排除软删除记录，本任务因此在 `api/assistants.py` 内自带带软删除过滤的唯一性检查；可在后续把它收进 conventions。
