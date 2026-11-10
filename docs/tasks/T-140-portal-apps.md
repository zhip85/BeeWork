# T-140 AI 门户 · 应用管理（管理员端）

**版本**：V0.4　**依赖**：T-024、T-070（来源选择与引用）；Chatflow / Workflow 来源在 T-075 合入后启用　**PRD**：演进 PRD 5.2、5.4、10.1–10.3、14.2–14.3、15；原型“AI 门户 → 应用管理 / 添加 AI 应用”

## 数据模型（`models/portal.py`）

`portal_apps`：`tenant_id`、`source_type`（`agent`（即现有助理）/ `chatflow_connection` / `workflow_connection`）、`source_id`、员工端名称（≤ 50）、描述（≤ 200）、分类（预置：效率工具 / 员工服务 / 销售 / 财务 / 法务，允许保留自定义值）、可见范围（`all` 全公司 / `orgs` 指定组织（关联表 `portal_app_organizations`）/ `admins` 仅管理员）、模式（`chat` / `form`；form 只对 Workflow 来源可选）、图标、`featured`、状态（`draft` / `listed` / `delisted`）、`listed_at`、`listed_by`、`delisted_reason`、`row_version`、审计字段、软删除。

## 权限

新增 RBAC 模块 `portal`（AI 门户）：`view` / `edit` / `publish`。按 PRD 6.1 的思路配置内置角色：租户管理员全部；构建者 view、edit；运营 view；查看者、审计员 view。补 403 测试。

## 规则

1. 新建时必须选**可用**来源：智能体须有线上版本（`live_version_id` 非空且未停用）；连接须启用且不失效。底层资源类型不展示给员工。
2. **已上架应用不可更换 `source_id` / `source_type`**（409 `portal.source_locked`）；要换就先下架再建新应用（演进 PRD 10.2）。
3. 上架检查（10.3）：来源存在且已发布 / 启用；名称等必填完整；可见范围有效（指定组织都存在于本租户）；来源不含失效能力引用（调用 T-070 `invalid_references`）。不满足返回 422 并列出原因。
4. 下架：员工端立即不可见、不可发起新调用；历史会话默认只读可看（演进 PRD 10.3 的建议值；做成门户设置项 `delisted_history: readonly | hidden`，默认 readonly，在 T-141 中）。
5. 来源后续被停用 / 删除：应用状态显示“来源不可用”，员工端隐藏（15.2），管理端保留配置。
6. 所有写操作写审计（新分类可归入“智能体”或新增“门户”，与审计页分类一起改）。

## 接口

`GET /portal/apps`（管理员列表，含来源状态）、`POST /portal/apps`、`GET/PUT/DELETE /portal/apps/{id}`、`POST /portal/apps/{id}/list`、`/delist`、`GET /portal/app-sources?q=`（可选来源：已发布智能体 + 启用的连接）、`GET /portal/summary`（已上架数等，指标部分由 T-147 填充真实值）。

## 界面

AI 门户页（管理端，导航“发布与运营 → AI 门户”）：页签“应用管理 / 门户设置 / 登录与身份 / 使用分析”（后三者由 T-141、T-146、T-147 实现，未完成前按占位规则显示）；应用卡片（名称、描述、分类、可见范围、状态、编辑）；添加 / 编辑抽屉（来源下拉、员工端名称、说明、分类下拉、可见范围下拉、模式、推荐、图标）；上架 / 下架确认弹窗说明影响。“预览员工端”在 T-142 后接入。

## 验收标准

- [ ] 测试：只能选已发布来源；已上架换来源 409；上架检查各失败项；下架后员工可见列表不含该应用（与 T-144 共用的服务函数）；跨租户 404；无权限 403；`row_version` 冲突 409。
- [ ] 历史数据兼容：`source_id` 为空的记录能正常读取并显示“来源未设置”，不 500（演进 PRD 10.2、14.3）。

## 完成记录

**改动文件**
- 后端：`models/portal.py`（`portal_apps`、`portal_app_organizations`）+ 迁移 `0041_portal_apps`（可降级）、`schemas/portal.py`、`portal_service.py`（来源解析器 / 候选 lister 注册表，现有 `agent`；上架检查 `listing_problems`；**可见性函数 `visible_apps(db, membership)`**，T-143 / T-144 直接复用）、`api/portal.py`、`rbac.py` 新增模块 `portal`（view / edit / publish）、审计动作 `portal_app.*`（归“智能体”分类）。
- 接口：`GET /portal/summary`、`GET /portal/app-sources`、`GET/POST /portal/apps`、`GET/PUT/DELETE /portal/apps/{id}`、`POST /portal/apps/{id}/list`、`/delist`。
- 权限（按 PRD 6.1 思路）：租户管理员 view / edit / publish；构建者 view / edit（不能上下架）；运营、查看者、审计员 view；`app-sources` 需要 edit。
- 规则：来源必须是**已发布且未停用**的智能体（有线上版本）；已上架应用换来源 409 `portal.source_locked`（草稿 / 已下架可以换）；上架检查逐项列原因（名称、来源已发布 / 未设置、可见范围里的组织存在、来源线上版本不含失效能力引用——按线上快照检查，员工实际会用到的才算）；表单模式只对工作流类来源开放，智能体来源选 `form` 被拒；同租户应用名不分大小写唯一；已上架的需先下架才能删除（软删除）；来源之后停用 / 删除，应用在管理端显示“来源不可用”并对员工隐藏，配置保留；`source_id` 为空的历史记录显示“来源未设置”，读取不报错。
- 与来源的联动：智能体“依赖”页列出基于它的门户应用；**被已上架门户应用使用的智能体不能删除**（`DELETE_BLOCKERS`）。
- 前端：导航“发布与运营 → AI 门户”（`/ai-portal`，`/portal` 留给员工端）、`pages/portal-admin/`（页签：应用管理可用，门户设置 / 登录与身份 / 使用分析显示“即将开放”；统计卡；应用卡片；添加 / 编辑弹窗：来源、名称、说明、分类下拉（保留历史自定义值）、可见范围（全公司 / 指定组织（含下级）/ 仅管理员）、推荐；上架 / 下架 / 删除确认），`i18n/{zh-CN,en}/portal.json`。月活跃员工与使用次数显示“–”，不编造数字（T-147）。
- 测试：`tests/test_portal_apps.py` 25 项（来源、校验、重名、组织归属、row_version、换来源锁定、上下架与可见性、失效来源、历史无来源记录、三种可见范围含组织下级、推荐排序、跨租户、删除、依赖与删除阻止、概要、内置角色权限矩阵、403、CSRF）。

**验证**：ruff；全量 `./scripts/test-postgres.sh`（迁移升降级 + `alembic check`）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 在开发库里走通“添加 → 草稿 → 上架确认 → 已上架”。
**是否连过真实第三方引擎**：不涉及。

**遗留问题**
- 来源目前只有智能体；Chatflow / Workflow 连接（T-075）合入后在 `portal_service.SOURCE_RESOLVERS` / `SOURCE_LISTERS` 注册即可出现在来源里，并支持表单模式。
- “下架后历史会话只读 / 隐藏”的门户设置项在 T-141，员工端行为在 T-143；本任务只保证下架后 `visible_apps` 不再返回该应用。
- 门户应用目前不作为引用方写入 `resource_references`（用 `DEPENDENCY_PROVIDERS` 与删除阻止覆盖智能体侧）；做连接 / 流程的引用提示时再决定是否并入。
- “预览员工端”按钮在 T-142 后接入；开发库里留有一个演示用的已上架应用“保险代理销售2”。
