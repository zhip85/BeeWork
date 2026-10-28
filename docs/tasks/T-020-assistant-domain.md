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
