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

## 完成记录（2026-11-12，Claude Sonnet 5.5）

**迁移**：`0049_flow_connections`（新表 `flow_connections`；可降级）。已在一次性 PostgreSQL 库上验证：升级 → `alembic check` 无差异 → 降级 → 再升级。

**后端**
- `models/flow_connections.py`、`schemas/flow_connections.py`、`flow_connection_service.py`、`api/flow_connections.py`：`GET/POST /flow-connections`、`GET/PUT/DELETE /{id}`、`/test`、`/enable`、`/disable`、`/references`、`GET /flow-connections/available-flows?kind=`；权限沿用 `capabilities:view/edit/publish/delete`；每个写操作有审计（`flow_connection.*`，详情不含凭据）与 CSRF。
- native：只能选**类型匹配且已发布**的画布（否则 422 `flow_connection.flow_unavailable`，跨租户画布同样拒绝）；没有时 `available-flows` 返回空数组（前端显示空态与“去流程画布”）。dify：`base_url`、`app_id`、API Key 缺一不可存（422）；`base_url` 走 `validate_public_https_url`（私网 / 回环 / 非 https → 400）；Key 经 `model_secrets` 加密，响应只有掩码，留空表示保持不变；删除时清除密文；Dify 来源需要套餐权益 `external_flows`（本平台画布不需要）。
- **失效是读时推导**（`state_of`）：native 连接的画布被停用 / 删除 / 未发布 / 类型不符 → `invalid` 并带原因；引用它的智能体在配置检查里出现 `mount.invalid`；本平台画布的 `references` 接口也会列出指向它的连接（`agent_service.REFERENCE_PROVIDERS`）。
- Dify 调用全部经 T-071 的 `safe_request`：只向用户填写的主机发送，连接固定到校验过的公网地址，不跟随重定向，响应 ≤ 1 MB。Chatflow → `POST /chat-messages`，Workflow → `POST /workflows/run`（blocking）。输入映射 `{Dify 变量: 智能体参数名}`，输出映射 `{field: 输出字段}`。错误映射：401/403 凭据被拒、404 应用不存在、429 限流、400 输入不符、5xx 上游错误、网络 / 超时 / 格式异常，并记录类别。
- 运行时：注册 `PROVIDERS["connection"]`（工具名 `connection:{id}`）、挂载解析器与候选列表（`chatflow` / `flow` 两个挂载类型，只列当前可调用的），`ToolMount.kind` 与 `ToolRuntime` 扩展到这两类，智能体把已挂载连接作为工具提供给模型。

**前端**：能力页新增 Chatflow / Workflow 两个页签（`FlowConnectionsTab`：卡片、状态、来源、引用数、启停、删除确认带引用数）与添加 / 编辑页（`FlowConnectionFormPage`：来源二选一、画布下拉与空态、Dify 字段、输入输出映射、API Key 密码态、测试连接与重试、最近测试结果）；中英文案各 80 余条。

**验证**：ruff；`tsc -b`、lint、`i18n:check`、`style:check`、`build`；`tests/test_flow_connections.py` 23 项新增 + 继承的 40 项流程测试全部通过；覆盖验收标准逐项：native 只能选同类型已发布画布、dify 缺字段 422、私网地址被拒、凭据只有掩码（响应 / 审计 / 数据库密文）、跨租户 404、无权限 403、画布停用后连接与引用方失效；`httpx.MockTransport` 契约测试覆盖 chat / workflow 调用与全部错误映射、密钥不泄漏、重定向不跟随。`mountable_kinds` 相关的 3 处旧断言同步更新。

**是否连过真实第三方引擎：没有。** 开发环境没有 Dify 应用的 API Key（`.env` 里的是知识库 API 密钥，不能调用应用），所以“用真实 Dify 测试应用跑通一次测试连接”**未完成**，只用 mock 传输验证；待你在 Dify 建一个 Chatflow 和一个 Workflow 并提供各自的 API Key 后补测。

**与规格的差异 / 遗留**
1. **本平台画布（native）暂时只能保存、检查，不能被调用**：画布运行时在 T-062–T-064 才有。native 连接的“测试”只检查画布是否已发布且启用；作为工具被调用时返回 `tool.flow_runtime_unavailable`。T-062 完成后在 `FlowConnectionProvider.execute` 接入即可。
2. 规格没有写启用 / 停用是否走审批，这里**直接生效**（不同于 MCP 的“启用第三方”审批），如需审批要另定。
3. 连接工具标为非写操作（`writes=False`）：由管理员选定并挂载，调用结果是给模型的文本；若某个 Dify 工作流会改数据，需要在后续任务里加“按连接声明写操作”。
4. `streaming` 开关只保存配置，调用仍是 blocking。
5. `app_id` 仅作标识保存（Dify 的 API Key 本身就绑定应用）；不影响调用。
6. 门户应用（T-140）把连接作为来源、OC 路由（T-058）引用连接，各自任务里接入；删除连接时不阻止（前端确认框列引用数），引用方在配置检查里显示失效。

## 评审修正（2026-11-12）

与 T-073 同类的问题：只改 Dify 连接的 Base URL 主机而把 API Key 留空，会把已保存的 Key 发到新主机。现在更换主机必须重新填写 API Key（422 `flow_connection.credential_reentry`）；同主机改路径不受影响；新增回归测试 `test_pointing_a_dify_connection_at_another_host_needs_the_key_again`。T-072 的 MCP 连接有同样的问题，本次未改。
