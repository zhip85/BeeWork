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

## 需求变更（2026-11-10，返工）

依据演进 PRD（`docs/PRD-ai-portal-and-agent-evolution.md`）与原型 `docs/ai-portal-prototype.html`：

1. **本任务改为“流程画布”的领域基础**：只管理 Chatflow 与 Workflow。“智能体 / Agent”就是现有助理（T-067），不再在 `agents` 表里建 `kind=agent` 的记录；`is_team`、`AgentConfigV1`、协作组检查删除（多智能体改为 T-066 的一层叶子协作）。表名 `agents` 与权限码 `agents:*` 保留（内部标识，界面上称“流程画布”）。
2. **入口**：导航“构建 → 流程画布”，路由 `/flow-studio`（列表，`?type=chatflow|workflow`）、`/flow-studio/new`、`/flow-studio/:id`；页头“FLOW STUDIO / 流程画布 / 独立设计并发布可复用流程，供本平台或外部系统调用。”；两张类型卡（Chatflow、Workflow），默认 Chatflow。删除第一版在导航中的“智能体”入口。
3. **发布与版本**（演进 PRD 6.8）：“设为可用”改为“**发布流程**”，状态文案 草稿 / 已发布 / 已停用；每次发布生成不可变版本（`flow_versions`：版本号、快照、操作者、时间、变更说明），`available_config` 指向最新发布版本的快照；卡片显示版本号与“已发布 · API 可调用”。支持“复制为草稿”。
4. **引用**：Chatflow / Workflow 不再被智能体直接挂载，而是经能力中心的**流程连接**（T-075）被引用；引用查询需包含流程连接，删除 / 停用时列出引用数。节点里的“智能体”目标改为现有智能体（`assistants`，调用其线上版本）。环检测只在流程之间（Workflow 节点引用 Workflow / Chatflow）。
5. 第一版代码在分支 `task/T-060-agent-domain`（提交 aa95402，未合入 main），返工在该分支上继续，完成检查与验收标准按本节更新：同类型重名被拒 / 不同类型可重名；发布后再保存草稿不影响已发布版本；含失效引用不能发布；环被拒；跨租户 404；运营 403。

## 完成记录（返工后）

**改动文件**
- 后端：`models/agents.py`（`agents` 表只放 Chatflow / Workflow：`published_config`、`live_version_id`、`published_at`；新增不可变的 `flow_versions` 表）、`schemas/agents.py`（只有 `FlowConfigV1`；节点带 `target_kind`）、`agent_service.py`（解析与预置节点、配置检查 AGT-028、节点调用目标有效性、流程间引用与环检测、`publish_flow` 生成版本、`REFERENCE_PROVIDERS` 注册点）、`api/agents.py`（路径沿用 `/agents`，对应界面“流程画布”）、迁移 `0038_agents`（未合入过 main，直接按新结构改写，可降级）、审计动作 `flow.*`（归“智能体”分类）。
- 接口：`GET /agents`（`kind`、`q`、分页）、`/agents/summary`、`POST /agents`、`GET/PUT /agents/{id}`（`row_version`）、`/checks`、`/references`、`/versions`、`POST /publish`（变更说明）、`/duplicate`（复制为草稿）、`/disable`、`/enable`、`DELETE`。权限 `agents:view/edit/publish/delete`。
- 前端：`pages/flow-studio/`（`FlowsPage` 列表：Chatflow / Workflow 两张类型卡、搜索、复制、删除确认含引用数；`FlowCreatePage`；`FlowEditorPage`：名称内联编辑、保存草稿、发布流程（变更说明）、发布更新、停用 / 启用 / 复制、检查栏、节点概览（失效调用目标标红）、版本历史）、路由 `/flow-studio`、`/flow-studio/new`、`/flow-studio/:flowId`，导航“流程画布”接入真实页面；`i18n/{zh-CN,en}/flows.json`。
- 测试：`tests/test_agents.py` 30 项；`test_versions` / `test_knowledge_retrieval` 中 `mountable_kinds` 仍为 `["kb"]`（流程不再直接挂载）。

**与第一版的差异**：去掉 Agent 类型、协作组、`is_team`、模型 / 资源配置、`agent` / `chatflow` / `flow` 挂载解析器；状态改为 草稿 / 已发布 / 已停用；“设为可用”改为“发布流程”并生成不可变版本；节点调用目标改为已发布的智能体（`assistants`）或已发布的流程，连接器目标待 T-075；流程间调用的自引用与环被拒。

**验证**：ruff；`./scripts/test-postgres.sh` 全量（迁移升降级 + `alembic check` 无差异）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 走了“新建 → 检查配置 → 发布流程 → 列表显示 v1”，发布前用 SQL 补了开场白 / 兜底回复（编辑器暂不能改会话设置）。验收标准逐项有测试：同类型重名被拒 / 不同类型可重名；发布后再保存草稿不影响已发布版本；含失效引用不能发布；环被拒；跨租户 404；运营 403。
**是否连过真实第三方引擎**：不涉及。

**遗留问题**
- 编辑器主体（画布、会话设置、输入输出、触发方式、试运行）仍是占位，T-062–T-064；因此界面上暂时无法让 Chatflow / Workflow 通过检查，发布只能在后端补全配置后进行。
- “复制为草稿”只复制草稿配置，不复制版本历史。
- 引用提供者 `REFERENCE_PROVIDERS` 为空：流程连接（T-075）与门户应用（T-140）合入时需各自注册，删除 / 停用提示才会计入它们。
- 开发库中留有演示用“Policy chatflow T060”。

## 需求变更（2026-11-12）

自研画布取消：本任务已完成的画布骨架由 `T-169` 下线与清理。
