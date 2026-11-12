# T-070 能力目录、挂载选择器与引用完整性

**版本**：V0.2　**依赖**：T-033、T-060　**PRD**：3.5.6（CAP-050–054）、AST-022、AST-062–066、AST-070、GLB-083、5.4（RUL-010–013）、KNW-042

## 目标

把“哪些资源可挂载、谁引用了谁、哪些引用已失效”做成统一服务，避免每个模块各算一套。

## 设计

- 资源类型注册表：kb、agent、chatflow、flow、mcp、api、skill，每类提供 `list_available(db, tenant_id, q)`、`status_of(obj)`（CAP-051 状态映射）、`display_meta(obj)`。
- 引用表 `resource_references`：`tenant_id`、引用方（类型 + ID + 配置层：draft / live）、被引用方（类型 + ID）、关系（mount / route_target / node / connected_agent / driver / fallback）。由各模块在保存草稿、设为可用、发布版本时**整体重算并替换**该引用方的行（同一事务），不做增量。
- 服务：`references_to(target)`（删除/停用前提示数量与列表，RUL-012、KNW-042）、`invalid_references(referrer)`（GLB-083，供各配置检查调用）。

## 范围

1. 挂载选择器全部类型（AST-022，替换 T-020/T-033 中的占位）：类型列表与角标、搜索、全选/清空、候选元信息、已选摘要；只列可用资源；自建 Skill 未通过安全检查不出现。
2. 能力页签（AST-063–066）全部类型；智能体类显示“路由目标”；卸载被路由规则引用的能力时确认（AST-065，规则失效由 T-058 处理）。
3. 删除/停用资源时返回引用方数量，前端确认文案按 PRD（AGT-005、CAP-007、KNW-042）。
4. 助理“依赖”页签（AST-070）：引用本助理的渠道、团队、外部流程；只读，可筛选状态，点击跳转。
5. CAP-053：能力（Skill/MCP/API）变更对引用它的助理**立即生效**，不随助理版本锁定；在 architecture.md 记录这一例外。

## 验收标准

- [ ] 测试：每类资源的状态映射；引用重算后旧引用消失；被引用对象删除后 `invalid_references` 返回该项；跨租户资源不出现在候选中。

## 需求变更（2026-11-10）

- 资源类型注册表改为：`kb`、`agent`（被挂载的叶子智能体 = `assistants`，T-066）、`skill`、`mcp`、`api`、`chatflow_connection`、`workflow_connection`（T-075）。原 `chatflow` / `flow`（直接挂载画布）不再作为挂载类型——画布只能经流程连接被引用。T-060 第一版注册的 `agent` / `chatflow` / `flow` 解析器随 T-060 返工调整。
- 引用方类型增加：流程连接（引用画布）、门户应用（T-140，引用智能体或连接）、渠道发布配置。删除 / 停用前展示引用数量、对象与跳转入口；高风险场景（被已上架门户应用或已发布渠道使用）阻止删除（演进 PRD 15.2）。
- 每项挂载记录“允许的工具 / 操作”与“委派描述”（演进 PRD 7.6，T-061 / T-066 使用）。
- 停用能力后智能体不得发起新调用；历史版本和运行记录保留可追溯信息（7.6）。

## 完成记录

**改动文件**
- 后端：`models/capability_refs.py` + 迁移 `0040_resource_references`（可降级；**回填**现有智能体的草稿 / 线上版本挂载与流程节点引用，开发库验证：1 个知识库挂载得到 draft + live 两行）、`capability_catalog.py`（边的计算、`after_flush` 监听整体替换引用行、`references_to`、`invalid_references`）、`capabilities.py` 增加 `CANDIDATE_LISTERS`、`api/assistant_checks.py`（`GET /capabilities/types`、通用 `GET /capabilities/candidates?kind=`、`GET /assistants/{id}/dependencies`、`DEPENDENCY_PROVIDERS`；知识库候选改为注册的 lister）、`api/assistants.py`（挂载校验对所有已注册类型通用：新挂载必须是可用资源，已挂载的失效项不阻塞保存；每类上限 30，知识库 10；`mount_settings` 只能针对已挂载资源）、`schemas/assistants.py`（`MountSettings`：允许的工具与委派描述）、`knowledge_mounts.py` / `agent_service.py` 的引用数改读统一的表。
- 前端：`CapabilityPicker.tsx`（全部 7 种类型、角标为已选数、搜索、全选 / 清空、候选详情、跨类型已选摘要；未开放的类型显示“即将开放”）、`MountDialog` 按多类型保存并清理被卸载能力的 `mount_settings`、`DependenciesTab.tsx`（渠道 / 智能体 / 流程 / 门户应用，按状态筛选，点击跳转）、`assistants.json` 中英文案。
- 文档：`architecture.md` 新增“能力目录与引用”一节，记录 CAP-053 例外。
- 测试：`tests/test_capability_catalog.py` 15 项（边的计算、保存整体替换、live 层随线上版本、删除移除、租户隔离、复制、流程作为引用方、失效引用、任意已注册类型的挂载与限制、陈旧挂载不阻塞保存、`mount_settings`、types / candidates、依赖页与权限）。

**验证**：ruff；全量 `./scripts/test-postgres.sh`（迁移升降级 + `alembic check`）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看过挂载弹窗与依赖页。
**是否连过真实第三方引擎**：不涉及。

**遗留问题**
- 现在只有知识库真正注册了解析器与 lister；智能体（T-066）、MCP（T-072）、API 插件（T-073）、Skill（T-074）、流程连接（T-075）各自上线时注册，并在 `mount_settings` 的 UI 里加“允许的工具 / 委派描述”（本任务只做了后端字段与校验）。“自建 Skill 未通过安全检查不出现”由 T-074 的 lister 实现。
- “被已上架门户应用 / 已发布渠道使用的资源阻止删除”只有框架（`REFERENCE_PROVIDERS`、`DELETE_BLOCKERS`）：门户应用在 T-140，渠道对智能体的阻止删除沿用 T-040 已有的规则；知识库删除仍是“提示引用数后允许”。
- 路由规则的引用（`route_target`）与兜底默认能力（`fallback`）关系已预留在表里，由 T-058 写入。
- 引用表的行只在 flush 时重算；直接用 SQL 绕过 ORM 改 `draft_config` 不会更新它（应用代码没有这样的路径）。

## 需求变更（2026-11-12）

扩展为统一能力注册中心（工具级注册项、输入 Schema、意图描述、权限、超时与重试策略、召回），见 `T-160`。
