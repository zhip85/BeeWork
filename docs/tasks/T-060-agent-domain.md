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
