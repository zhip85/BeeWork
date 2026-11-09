# T-054 OC 配置框架与运行管线骨架

**版本**：V0.2　**依赖**：T-024、T-025　**PRD**：3.6.1（OC-001–007）、3.6.2、3.2.6、4.8、5.5、NFR-001、NFR-012

## 目标

建立 OC 的配置存储、页面框架和运行时“分阶段管线”，后续各阶段任务（T-055–T-059）只往里填自己的阶段。

## 配置存储（已定）

- OC 配置是助理配置的一部分：`draft_config.oc`（六个阶段各一个子对象，每个带默认值，Pydantic 定义，`oc_schema_version: 1`）；线上 OC = 线上版本快照中的 `oc`。不建独立的“OC 草稿/线上”表。
- **OC「提交审批」（OC-004）的生效方式**：新版本 = 当前线上快照 + 草稿中的 `oc` 部分（只合并 OC），走“助理规则变更”审批事件；这样 OC 审批不会顺带发布未审批的提示词/模型修改。助理从未发布时，OC 提交审批提示“请先发布助理”。

## 运行时

- 把 T-025 的图改成固定顺序的阶段管线：`ingress → admission → pii_input → memory → glossary_rewrite → route → execute → components → pii_output → egress`（5.5）。每个阶段是注册表中的一个节点，未实现的阶段为直通；每个阶段产生一个 Langfuse span（名称固定，供 T-084 瀑布图使用），记录耗时。
- 管线自身耗时（不含模型/工具）作为指标写入 run 记录，为 NFR-001（P95 ≤ 300 ms）验收准备。

## 页面

- 编排 OC 页框架（OC-001–007）：助理选择器（切换时未保存确认）、阶段导航（支持 URL 直达）、保存草稿、提交审批、权限（`orchestration:view/edit/publish`，无编辑权限只读并隐藏按钮）。
- 编排总览（3.6.2）：六行摘要，各阶段摘要由各阶段任务提供计算函数，未实现的阶段显示“即将开放”。
- 助理详情“编排”页签（3.2.6）：六阶段步骤条与两张摘要卡，点击跳转对应阶段。

## 验收标准

- [ ] 测试：OC 提交审批生成的新版本只改变 `oc`，提示词等仍为旧线上值。
- [ ] 测试：管线阶段顺序固定；某阶段抛错时 run 失败并在事件中带阶段名。
- [ ] Langfuse 中能看到按阶段划分的 span（有 Key 时）。

## T-054a 闭环切片先行范围（优先线）

**依赖**：T-025（不等 T-024）。只建运行时管线骨架，用草稿配置直跑，其余仍属本任务：

- **做**：把 T-025 的图改为固定阶段管线与阶段注册表（顺序见上，未实现阶段直通）；每阶段一个 Langfuse span（有 Key 时）；管线自身耗时写入 run 记录；`draft_config.oc` 的 Pydantic 结构（`oc_schema_version: 1`，六阶段默认值）；某阶段抛错时 run 失败且事件带阶段名。`memory` 阶段提供挂点，供 T-056a 填充。
- **不做（留给 T-054 剩余部分，依赖 T-024）**：OC 页面与编排总览、助理详情“编排”页签、OC 提交审批与“只合并 OC 到线上快照”、权限接入。
- 运行时仍通过 T-025 的 `ConfigSource` 读取草稿，不绕过版本机制；T-024 加入后改为读快照。
- 测试：阶段顺序固定、阶段抛错带阶段名、未实现阶段直通。

### T-054a 完成记录

**改动文件**：新增 `runtime/oc.py`、迁移 `0019_runs_pipeline.py`（`runs.pipeline_ms/stage_ms/error_stage`，可降级）；重写 `runtime/compiler.py`（十阶段管线、`register_stage`、`StageClock`）；修改 `runtime/{dsl,executor,tracing,errors}.py`、`models/runtime.py`、`schemas/{assistants,runs}.py`、`tests/test_runtime.py`（+8 项）、`tests/test_assistants.py`（+1 项）、`scripts/check_runtime_e2e.py`。

**验证**：`ruff`、`./scripts/test-postgres.sh`（迁移升降级 + `alembic check` + 135 项）、前端检查通过；真实栈 `check_runtime_e2e.py` 通过：每个阶段一个 Langfuse span、`pipeline_ms` 已记录且远小于总耗时、元数据与脱敏校验仍通过。仍是 `mock_response` 模型 + 检索桩。

**遗留问题**
- 只做了骨架：`admission`（运行时再次准入）、`pii_input`/`pii_output`、`glossary_rewrite`、`components` 是直通；`route` 固定到助理自身；`memory` 等 T-056a。
- 未做（依赖 T-024）：OC 页面、编排总览、助理详情“编排”页签、OC 提交审批与“只合并 OC 到线上快照”、权限接入。
- `pipeline_ms` 的口径：各阶段墙钟时间减去 `execute` 里模型/检索调用的耗时；流式输出时事件落库的开销落在模型调用的墙钟里，没有计入管线耗时。NFR-001（P95 ≤ 300 ms）的正式压测在 T-125。
- OC 各阶段的默认值（`glossary/rewrite/routing/components` 默认关闭，PII 与记忆默认开启）是本任务定的，后续阶段任务落地时需确认。
- 阶段 span 嵌在 `assistant.run` 下，`ChatOpenAI` 生成观测嵌在 `oc.execute` 下，依赖 LangGraph 在线程间传递 OpenTelemetry 上下文（当前版本如此）。
- 十阶段与旧的 `input_guard/retrieve/generate/output` 步骤名不同，`step` 事件的 `name` 变化；还没有前端消费者。

### T-054 剩余部分完成记录

**改动文件**
- 后端：新增 `assistant_oc.py`（OC 草稿/线上读取、`with_oc`、审批动作 `assistant.oc_change`、阶段摘要注册表）、`schemas/assistant_oc.py`、`api/assistant_oc.py`、`tests/test_assistant_oc.py`（17 项）；修改 `assistant_versions.py`（`oc_change` 版本的审计动作）、`schemas/versions.py`、`models/assistants.py`（注释）、`models/__init__.py`（审计文案）、`api/__init__.py`、`worker/runner.py`（注册审批动作）、`tests/test_versions.py`（修复一个会把真实 `kb` 挂载解析器弹出注册表的测试清理，只影响测试顺序）。
- 前端：新增 `pages/orchestration/{OrchestrationPage,OrchestrationTab}.tsx`、`stagePanels.ts`、`types.ts`、`i18n/*/orchestration.json`；修改 `App.tsx`（路由 `/assistants/:id/oc[/:stage]`）、`AssistantDetailPage.tsx`（“编排”页签）、`VersionsPage.tsx`（`oc_change` 标记）、`App.css`。
- 迁移：无（`assistant_versions.kind` 是字符串列，新增取值 `oc_change`）。

**接口**：`GET /oc/assistants`（助理选择器）、`GET|PUT /assistants/{id}/oc`（读取 / 保存草稿，带 `row_version`）、`POST /assistants/{id}/oc/submit`（201 直接生效 / 202 待审批）。权限 `orchestration:view|edit|publish`，另受助理范围角色限制。

**设计**
- OC 提交审批走审批事件 `rule`（“助理规则变更”，默认安全员 1 级、开启），请求类型 `rule_change`，`object_type=assistant_oc`，这样 OC 的待审批不会让助理显示“审批中”，也不占用发布的待审批名额。
- 新版本 = 申请**通过时**的当前线上快照，只替换 `config.oc` 与 `dsl.oc`（`with_oc`）；提示词、模型、挂载的草稿修改不会被带上，审批期间发生的其他发布也不会被覆盖。助理从未发布时提交返回 409 “请先发布助理”。
- 各阶段摘要由 `assistant_oc.register_summary(stage, fn)` 注册，前端 `STAGE_PANELS` 注册各阶段编辑页；未注册的阶段显示“即将开放”。

**验证**：ruff；全量后端 524 项通过（迁移升降级 + `alembic check` 无变化）；前端 `tsc -b`、lint（0 警告）、`i18n:check`、build 通过。验收标准逐条：
- “OC 提交审批生成的新版本只改变 `oc`，提示词等仍为旧线上值”——`test_policy_off_applies_directly_and_only_oc_changes`、`test_the_approval_applies_only_the_oc_and_keeps_a_later_publish`。
- “管线阶段顺序固定；某阶段抛错时 run 失败并在事件中带阶段名”、“未实现阶段直通”——T-054a 已有测试（`test_runtime.py`），本次未改动运行时。
- “Langfuse 中能看到按阶段划分的 span”——T-054a 在真实栈上已验证（12 个 observation），本次未改动运行时，未重跑。
- 另覆盖：保存只写草稿并审计、校验与陈旧版本 409、审批通过/驳回/替换待审批、停用助理、助理选择器范围、查看者 403、跨租户 404、未登录 401。
**是否连过真实第三方引擎**：本任务不涉及。**没有在浏览器里目视检查 OC 页面、选择器与“编排”页签。**

**遗留问题**
- 六个阶段的编辑页与摘要都还没有（T-055–T-059 提供），所以“保存草稿 / 提交审批”按钮目前只能保存默认值，页面上无法真正改动 OC；后端流程已用 API 测试验证。
- 详情“编排”页签下方的两张摘要卡（护栏条数、记忆文件数）需要 PII / 记忆阶段的数据，等 T-055 / T-056 再补，现在只显示六步条和草稿与线上是否一致。
- 页面左栏阶段分组沿用 PRD 的 4 组 6 阶段；“下期置灰”标记没有做（当前所有阶段都可点，进入后显示“即将开放”）。
- OC 审批通过后的通知复用 `assistant.published` 类型，文案为“OC 变更已生效”。
- 侧栏的“工作流”入口（`/workflows`，权限 `orchestration:view`）仍是占位页，与本任务无关。
