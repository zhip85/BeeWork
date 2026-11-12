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
