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
