# T-075 流程连接（Chatflow / Workflow 连接）

**版本**：V0.2　**依赖**：T-060、T-070、T-071　**PRD**：演进 PRD 7.1、7.6、8（全部）、15.2、16.2；原型“能力 → Chatflow / Workflow → 添加连接”
**外部条件**：Dify 应用的 API Key（开发环境已有 Dify Cloud，可建一个测试应用）

## 背景

演进 PRD 把“设计流程”和“调用流程”拆开：流程画布（T-060、T-062–T-064）只负责设计和发布；能力中心的 Chatflow / Workflow 页签是**连接**，把一个已发布画布或一个外部应用包装成可调用资源，供智能体挂载、OC 路由和门户应用使用。

## 数据模型（`models/flow_connections.py`）

`flow_connections`：`tenant_id`、`kind`（chatflow / workflow）、名称（同租户同类型唯一，≤ 50）、说明、`source_type`（`native` 本平台画布 / `dify`）、`flow_id`（native 时指向 `agents` 表中已发布的流程）、外部配置（`base_url`、`app_id`、`credential` 加密存储（`model_secrets`），响应只返回掩码）、`input_mapping` / `output_mapping`（JSON）、`streaming`、状态（enabled / disabled）、最近测试时间与结果、`row_version`、审计字段、软删除。

## 接口与权限

- 权限沿用 `capabilities:view/edit/publish/delete`。
- `GET /flow-connections?kind=&q=`、`POST`、`GET/PUT/DELETE /flow-connections/{id}`、`POST /{id}/test`、`/enable`、`/disable`、`GET /{id}/references`。
- native：只能选**类型匹配且已发布**的画布；没有时返回明确空态（原型“仅展示类型匹配且已发布的画布”）。无需凭据。
- dify：`base_url` 走 `validate_public_https_url`（NFR-009）；`base_url`、`app_id`、凭据任一缺失不可保存（422）。凭据只在服务端解密使用，日志、审计、错误信息不出现明文（architecture 3.5）。
- 测试连接：经 T-071 出口执行器调用一次 Dify 应用（Chatflow 用 chat-messages，Workflow 用 workflows/run），记录耗时、结果和错误类别；失败显示原因、最近测试时间和重试入口（演进 PRD 8.4）。

## 运行时

- 注册为能力类型 `chatflow_connection` / `workflow_connection`（T-070 注册表），可被智能体挂载、被 OC 路由选中、被门户应用作为来源。
- native：调用画布的**已发布配置**（RUL-002）；dify：经 T-071 调用，按映射组装输入、解析输出；超时、重试、限流在 T-071 统一处理。
- 失效（演进 PRD 8.4、15.2）：底层画布被停用或删除、连接被停用 → 连接状态显示“失效”，引用它的智能体和门户应用在各自配置检查中列为失效引用；不静默忽略。

## 界面

- 能力页 Chatflow / Workflow 页签：连接卡片（名称、来源“本平台画布 / Dify”、状态、可供智能体和应用调用）、添加连接、配置、启停、删除确认（列引用数）。
- 添加连接：来源二选一（本平台已发布画布（默认推荐）/ Dify 应用）；native 选流程下拉；dify 填 Base URL、App ID、API Key（密码态）、输入输出映射、流式开关；保存前字段级校验。

## 验收标准

- [ ] 测试：native 只能选同类型已发布画布；dify 缺字段 422、私网地址被拒；凭据响应只有掩码；跨租户 404；无权限 403；底层画布停用后连接与引用方显示失效。
- [ ] 契约测试（`httpx.MockTransport`）覆盖 Dify chat / workflow 调用与错误映射。
- [ ] 用真实 Dify 测试应用跑通一次“测试连接”，记录在 development-status（无应用时如实写明）。

## 不在范围

- Coze、n8n、通用 HTTP 等其他外部来源（2026-11-10 确认不在当前计划内，不预留界面入口）。
- 流程画布本身的设计与发布（T-060、T-062–T-064）。
