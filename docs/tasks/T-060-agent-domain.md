# T-060 智能体领域模型、列表、创建页与编辑器框架

**版本**：V0.2　**依赖**：T-001、T-010、T-025　**PRD**：3.3、3.3.1（AGT-001–007）、3.3.2、3.3.3（AGT-020–028）、4.5、5.1 智能体状态机、RUL-002、RUL-011–014

## 数据模型（`models/agents.py`）

- `agents`：按 PRD 4.5；`kind`（agent / chatflow / workflow）、`is_team`（多智能体协作组，以 Agent 类型呈现）、名称（同租户同类型唯一）、描述、图标、状态（draft / available / disabled）、`draft_config`、`available_config`（RUL-002：引用方与线上始终用可用配置，调试与试运行用草稿）、`available_at`、`row_version`、审计字段、软删除。
- 配置结构用 Pydantic 定义并带 `dsl_version: 1`：`AgentConfigV1`（指令、用途描述、模型、资源引用、记忆开关、协作组设置）与 `FlowConfigV1`（节点列表、Chatflow 会话设置、Workflow 输入输出与触发）。节点类型的细节在 T-062 补充，本任务先定义外壳。
- 无法解析的配置不覆盖原数据（AGT-007、GLB-084）：读取时校验失败返回 `config_corrupted=true`，编辑器拒绝进入。

## 接口与权限

- `agents:view/edit/publish/delete`（6.1：管理员、构建者全权；运营、查看者、审计员只读）；组织范围过滤。
- `GET /agents?kind=&q=`、`POST /agents`（创建为草稿，模型取当前环境默认对话模型，按类型预置节点 3.3.2）、`GET/PUT /agents/{id}`（保存草稿，`row_version`）、`POST /agents/{id}/make-available`（配置检查通过后把草稿复制为可用配置）、`/disable`、`/enable`、`DELETE`（引用方显示失效）、`GET /agents/{id}/checks`。
- 配置检查（AGT-028）：本任务实现通用规则与 Workflow/Chatflow 的字段级规则；节点完整性规则由 T-062 注册进同一检查器。
- 环检测（RUL-014）：保存连接关系时检测 Agent ↔ Agent / Workflow 的引用环，有环拒绝保存。
- 写审计；分类“助手”（或新增“智能体”分类，与审计页分类一起改）。

## 界面

- 列表页（AGT-001–006）：三类页签卡（URL 同步）、搜索、卡片、删除确认、空态。
- 创建页（3.3.2）：名称 ≤ 50、描述 ≤ 200、8 个图标。
- 编辑器外壳（AGT-020–027）：返回确认、名称内联编辑、保存状态、保存草稿、设为可用、停用/启用、检查配置栏、护栏提示。各类型的编辑主体由 T-061–T-064 实现，先放占位。

## 验收标准

- [ ] 测试：同类型重名被拒、不同类型可重名；已可用对象保存草稿后可用配置不变；含失效引用不能设为可用；环被拒绝；跨租户 404；运营不能编辑（403）。

## 完成记录

**改动文件**
- 后端：`models/agents.py`（`agents` 表）、`schemas/agents.py`（`AgentConfigV1` / `FlowConfigV1` 等带 `dsl_version: 1` 的配置结构）、`agent_service.py`（配置解析与预置节点、配置检查 AGT-028、引用查询、环检测、`agent` / `chatflow` / `flow` 挂载解析器）、`api/agents.py`；在 `models/__init__.py`、`api/__init__.py` 各注册一行；审计动作 `agent.*` 归入“助手”分类。迁移 `0038_agents`（可降级）。
- 前端：`pages/agents/`（`AgentsPage`、`AgentCreatePage`、`AgentEditorPage`、`AgentIcon`、`types`）、`i18n/{zh-CN,en}/agents.json`、导航“智能体”与三条路由、样式追加在 `App.css`。
- 测试：新增 `tests/test_agents.py`（33 项）；`test_versions` / `test_knowledge_retrieval` 里两处断言的 `mountable_kinds` 随解析器注册改为 `["agent","chatflow","flow","kb"]`。

**接口**：`GET /agents`（`kind`、`q`、分页）、`GET /agents/summary`、`POST /agents`、`GET/PUT /agents/{id}`（`row_version`）、`GET /agents/{id}/checks`、`GET /agents/{id}/references`、`POST /agents/{id}/make-available | disable | enable`、`DELETE /agents/{id}`（软删除）。权限 `agents:view/edit/publish/delete`（已在 rbac 中，运营 / 查看者 / 审计员只读）。

**验证**：ruff；`./scripts/test-postgres.sh`（迁移升降级 + 全量 742 项，首轮仅上述两处断言需随解析器更新，更新后通过）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看过列表、创建页、编辑器与检查栏，并在开发库实际创建了一条智能体。验收标准逐项有测试：同类型重名被拒 / 不同类型可重名；可用后保存草稿，可用配置不变；含失效引用不能设为可用；自引用与环被拒；跨租户 404；运营 403。
**是否连过真实第三方引擎**：不涉及。

**可逆决定**
- 已可用的对象保存草稿后状态保持“可用”（页面提示有未设为可用的草稿修改）；按状态机字面改成“草稿”会让引用方立刻失效，与 AGT-024 / RUL-002 矛盾。
- 智能体不带组织归属（PRD 4.5 无该字段），不做组织范围过滤。
- 渠道 / 技能 / 工具引用在能力目录（T-070 起）就绪前一律视为失效。
- 协作组检查：至少连接 2 个智能体，主控需在其中。

**遗留问题**
- 编辑器主体（指令与资源、编排、会话设置、输入输出、触发、试运行）按规格是占位，由 T-061–T-064 实现；当前编辑器只能改名称、状态和可用配置，保存草稿不会改动配置。
- 节点完整性规则（AGT-045）留有注册点 `agent_service.NODE_RULES`，由 T-062 填充。
- 运行时嵌套深度 ≤ 5（RUL-014）未做，待运行时任务。
- 开发库里留有一条演示用“Support agent T060”。
