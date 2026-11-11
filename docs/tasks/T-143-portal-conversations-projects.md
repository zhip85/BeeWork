# T-143 员工端对话与项目

**版本**：V0.4　**依赖**：T-142、T-140、T-025、T-026　**PRD**：演进 PRD 11.2、11.3、18.6
**外部条件**：供应商模型 Key（真实回答验收需要；无 Key 时用 `mock_response` 并如实记录）

## 范围

1. **对话**：员工选择一个可见的门户应用（或门户设置中的默认应用）发起对话；消息经该应用的底层来源真实运行——来源是智能体时走现有运行入口（冻结线上版本、OC 管线、流式事件），来源是连接时走 T-075。运行记 `source=portal`，`end_user` 绑定员工的成员身份。**不写前端模拟回答**。
2. 复用现有 `conversations` / `messages` / `runs`（T-025、T-026）：新增门户渠道类型 `portal`，必要时加 `project_id`、`pinned_at`、`title` 字段；不另建一套会话表（若确需拆表，先在任务文件“问题”一节说明）。
3. 员工端接口（`/api/v1/portal/me/*`）：对话列表（搜索、置顶在前、最近使用排序）、新建、发送消息（SSE 流式，复用 `/runs/{id}/events`）、重命名、置顶 / 取消置顶、删除（员工自己的对话软删除）；只能访问自己的对话（他人 404）。
4. **项目**：`portal_projects`（tenant、owner 成员、名称、说明）；列表、新建、详情、编辑、删除；对话可关联项目；项目内新建对话。门户设置关闭“项目”时接口 403（T-141）。
5. 输入：Enter 发送、Shift+Enter 换行；发送中禁用；失败可重试。
6. 应用已下架 / 来源不可用：新消息返回 410 `portal.app_unavailable`，界面明确提示；历史会话按 T-141 `delisted_history` 只读或隐藏。

## 验收标准

- [ ] 测试：员工看不到他人的对话与项目（404）；对不可见或已下架应用发消息被拒；关闭项目 / 置顶后接口 403；运行使用应用来源的线上版本而不是草稿；`source=portal` 计入用量。
- [ ] 浏览器里完成一次“新建对话 → 发送 → 收到回答 → 置顶 → 移入项目 → 删除”，记录是否为真实模型。

## 需求变更（2026-11-11）

- 所有员工端接口还要挂上 T-151 的 `require_portal_seat`（无门户席位 403 `portal.seat_required`；全员不限的租户除外）。若 T-151 晚于本任务合入，在 T-151 里补挂并补测试。

## 已确认（2026-11-11）

租户管理员**默认不能**查看员工的门户对话与项目；管理端的会话查看只覆盖智能体 / 渠道会话。是否提供“允许管理员查看”的租户设置，等有合规需求再做，本任务不做。

## 完成记录（2026-11-11，Claude Sonnet 5.5）

**迁移**：`0047_portal_chat`（可降级）：新表 `portal_projects`（租户、所有者 `user_id`、名称、说明、软删除）；`conversations` 增加 `title`、`portal_app_id`、`project_id`、`pinned_at`、`deleted_at` 与索引 `ix_conversations_portal_owner`。沿用现有 `conversations` / `messages` / `runs`，没有另建会话表。

**运行**：员工消息走与开放 API / 挂件相同的真实路径 `channel_chat.resolve_target`（线上环境 → **冻结的已发布版本，不用草稿**；模型 / 套餐 / 点数配额 / 环境检查）→ `start_chat`（`source=portal`，EndUser `channel_type=portal`、`external_id=成员 user_id`，即绑定员工的成员身份）→ Worker `runtime.execute`（OC 管线、检索、Langfuse 追踪）→ SSE。新增 `on_create` 钩子让新对话在提交前写入标题 / 应用 / 项目。门户对话计入会话配额与用量（`source=portal`，不在不计费来源里）。来源不是智能体（连接，T-075）暂时都按“来源不可用”处理。

**接口**（`/portal/me/*`，全部挂 `require_portal_seat`）：`GET apps`（应用选择器；完整应用库与分类在 T-144）、`GET conversations`（搜索、项目筛选，置顶在前再按最近使用）、`POST chat`（SSE 流式或一次性，新建或续聊）、`GET conversations/{id}/messages`、`PATCH conversations/{id}`（重命名 / 置顶 / 移入项目 / 移出项目）、`DELETE conversations/{id}`（员工自己的软删除，保留期仍按租户策略清理；写审计）、`GET/POST /projects`、`GET/PATCH/DELETE /projects/{id}`（删除项目保留其中的对话；都写审计）。只能访问自己的对话与项目，他人 404；**管理端会话列表 / 详情现在排除 `source=portal`**（租户管理员看不到员工对话，已确认的决定）。门户设置关闭“项目 / 置顶 / 搜索”时对应操作 403 `portal.feature_disabled`；应用下架或来源不可用：新消息 410 `portal.app_unavailable`，历史按 `delisted_history` 只读或隐藏；应用不存在 / 草稿 / 不在可见范围 → 404。

**前端**：员工端首页即新对话（问候 + 居中输入 + 应用选择），Enter 发送、Shift+Enter 换行、发送中禁用、失败可重试；对话页（流式显示、引用、下架后只读横幅）；侧栏置顶 / 最近列表与每条对话的菜单（重命名 / 置顶 / 移入或移出项目 / 删除并二次确认）；搜索页；项目列表、项目详情（编辑、删除、在项目内新建对话、项目内对话）。收到 `portal.seat_required` 时外壳回到无席位页（补上 T-151 遗留项 2）。中英文齐全。

**验证**：`ruff` 通过；`test_portal_chat`（20 个）+ `test_portal_seat_check`（含“所有 /portal/me 路由都挂席位依赖”的枚举，现已覆盖本任务全部接口）、`test_portal_seats`、`test_portal_apps`、`test_portal_settings`、`test_conversations`、`test_open_api`、`test_channels`、`test_usage`、`test_runtime`、`test_employee_role`、`test_postgres_integration` 共 240 个通过，迁移升降级与 `alembic check` 通过。测试覆盖：运行使用线上冻结版本而不是草稿、`source=portal` 与成员绑定、流式 start / delta / done、续聊、会计入会话用量、他人 404 与管理员不可见、跨租户 403、重命名 / 置顶排序 / 搜索 / 软删除与审计、项目隔离与移入移出、关闭项目 / 置顶 / 搜索后 403、应用下架 / 来源不可用 410、历史只读 / 隐藏、草稿 / 范围外 404、无席位 403、参数校验与 CSRF。前端 `tsc` / `lint` / `i18n:check` / `style:check` / `build` 通过；浏览器（开发库，真实员工账号）完成“新建对话 → 发送 → 收到回答 → 置顶 → 新建项目 → 移入项目 → 删除”，并看了手机宽度。

**是否真实模型：否。** 开发库里的聊天模型是 E2E 用的 `mock_response` 网关模型（回答是固定文本“根据知识库，购买后 7 天内可以申请无理由退货。”），所以验证到的是：员工端 → 冻结版本运行 → LiteLLM 网关 → Dify 检索（`retrieval_mode=dify`）→ Langfuse → SSE → 前端展示这一整条真实链路，模型输出本身是 mock。拿到供应商 Key 后需要用真实模型再验一次。

**遗留 / 说明**
1. 回答风格（简洁 / 均衡 / 详细）：服务端运行入口没有对应参数，我没有在对话里放一个假的选择器，T-142 首页原有的风格下拉也一并去掉；需要时由产品定义它如何影响提示词，再接入。
2. 门户设置没有“默认应用”字段，所以发起对话必须选择应用（应用选择器只有一个应用时自动选中）。
3. 流式响应不支持断线续传：刷新后通过消息接口读取历史；进行中的回答在刷新后需要等待完成再看到。
4. 员工端没有对话 QPS 限流（只有套餐的会话 / 点数配额）；如需防滥用另做。
5. `jsx` 的 react-hooks 新增 3 条 lint 警告（effect 内 setState），不影响构建。
6. 未跑全量后端测试（主机负载高）；合并前补跑。开发库里留下了员工演示账号的几条对话（均为 mock 回答），“项目”开关已打开。
