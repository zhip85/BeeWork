# T-169 下线自研画布

**版本**：V0.5（运行时架构 v2）　**依赖**：T-163　**PRD**：演进 PRD 23（6.5、8.1–8.2 作废）；`architecture-runtime-v2.md`

## 目标

2026-11-12 决定取消自研流程画布（工作流统一用 Dify）。T-062–T-065 取消；T-060 已完成的画布骨架（列表、创建页、编辑器框架、发布版本）要从产品里下线并清理，避免留下用户可见的死入口与无人维护的代码。

## 范围

1. **前端**：移除“流程画布”导航与路由（`/flow-studio*`）、`pages/flow-studio/`、依赖页里指向画布的项、流程连接页残留的画布文案；`i18n`（中英）与 `App.css` 里对应的键 / 样式；角色权限矩阵页里的“流程画布”模块行隐藏。
2. **后端**：移除 `api/agents.py`（`/agents*`）、`agent_service.py` 及其注册（引用提供者、`REFERENCE_PROVIDERS`）、`capability_catalog` 里 `Agent` 的引用重算（`sync_flow`、`flow_edges`）与 `references_to` 里 `flow` 的分支；`TARGET_FOR_MOUNT` 的 `flow` 含义保持“Workflow 连接”不变（只是不再有画布）；`models/agents.py`（`agents`、`flow_versions`）。
3. **迁移**：删除 `agents`、`flow_versions` 表与 `flow_connections.flow_id`（含外键）；升级前检查——存在未删除的画布数据时迁移**拒绝执行并给出数量**（先导出 / 清理），不静默丢数据；迁移可降级（重建空表与列）。
4. **权限**：`agents:*` 权限码保留为已弃用（不再有任何接口检查它，避免自定义角色里已授予的权限数据迁移），在角色管理页不再显示；`architecture.md` 记录。
5. **门户应用 / 渠道**：确认 `portal_apps.source_type` 没有画布来源（只有 `agent`、`chatflow_connection`、`workflow_connection`）；如发现引用画布的数据，按 5.3 的拒绝策略处理。
6. **文档**：`development-status.md`（模块状态里移除画布）、`technical-plan.md`（阶段 5 去掉画布、T-062–T-065）、`architecture.md`、`README.md`、看板；T-060 的完成记录加“已下线（T-169）”。
7. **测试**：删除 `test_agents.py`（画布）；补一个迁移测试（有 / 无画布数据）；确认全量测试与前端检查通过，没有对 `agents` / `flow_versions` 的残留引用（`grep` 检查写进验收）。

## 验收标准

- [ ] 全量后端测试、前端 `tsc` / lint / `i18n:check` / `style:check` / 构建通过；PostgreSQL 迁移升级 → `alembic check` → 降级 → 升级通过。
- [ ] 有画布数据时迁移拒绝并报告数量；无数据时成功。
- [ ] 代码库里不再引用 `models.agents`、`agent_service`、`/api/v1/agents`、`flow-studio`（除历史迁移文件与文档的历史记录）。

## 完成记录（2026-11-12，Claude Sonnet 5.5）

**迁移** `0051_retire_flow_canvas`（可降级）：升级前检查未删除（`deleted_at IS NULL`）的画布，**有则拒绝并报告数量**，设置环境变量 `BEEWORK_DROP_FLOW_CANVAS=1` 确认丢弃后才执行（软删除的不阻塞）；删除画布的引用记录（`referrer_type` 或 `target_type` 为 `flow`）；遗留的 `native` 流程连接置为停用；删除 `flow_connections.flow_id`（外键与索引）、`flow_versions`、`agents`。降级重建空表与列。

**后端**：删除 `api/agents.py`、`agent_service.py`、`models/agents.py`、`schemas/agents.py`、`tests/test_agents.py`；`capability_catalog` 去掉画布的引用重算与 `references_to` 里的画布分支；流程连接服务 / 接口去掉 native 分支（创建或改成 native 返回 422 `flow_connection.source_retired`，遗留 native 连接为 `invalid` 且不可调用、不出现在候选里，可改成 Dify 连接）、删除 `available-flows` 接口与 `flow_id` / `flow_name` 字段；`rbac` 增加 `DEPRECATED_MODULES`：`agents` 权限码保持合法但角色矩阵不再列出。

**前端**：删除 `pages/flow-studio/`、导航与路由、`flows` 文案命名空间、画布专用 CSS（37 条规则）、依赖页的画布项、流程连接页的来源选择与画布下拉；中英文案同步。

**验证**：ruff；PostgreSQL 迁移升级 → `alembic check` → 降级 → 升级；`test_retire_flow_canvas_migration` 4 项（有活画布拒绝并报数量且不丢数据、只有软删除的画布可直接执行、确认后删除画布与引用并停用 native 连接、降级后重建且 `alembic check` 一致）；`test_flow_connections` 改为独立的 Dify 测试并新增 2 项（native 不可再创建 / 切换，遗留 native 失效、不可调用、不是候选）；`test_capability_catalog` 去掉画布用例；前端 tsc / lint / i18n / style / build；`grep` 确认代码库里不再引用 `models.agents`、`agent_service`、`/api/v1/agents`、`flow-studio`（历史迁移与文档的历史记录除外）；全量后端见看板。

**注意 / 遗留**
1. **开发库如有画布数据**（例如“Policy chatflow T060”演示数据），执行迁移时会被拒绝；确认丢弃后用 `BEEWORK_DROP_FLOW_CANVAS=1` 重试。
2. 历史迁移 `0038` / `0040` / `0049` 里仍有画布相关语句，这是历史记录，不改。
3. `agents:*` 权限码仍存在于内置角色的权限集合里（已弃用，无接口检查）；如要彻底清除，需另做一次角色数据迁移。
4. 本任务的依赖写的是 T-163，但只做了其中“去掉 native 来源”一项（下线画布必需）；T-163 的其余部分仍待做。
