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
