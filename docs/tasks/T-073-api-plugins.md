# T-073 API 插件

**版本**：V0.2　**依赖**：T-072　**PRD**：3.5.5（CAP-040–042）、3.5.2

## 范围

1. `api_plugins`：类型 REST / GraphQL、认证方式（None / API key（Header 名 + 值）/ Bearer / OAuth 2.0 客户端凭证（Token URL、Client ID、Client Secret、Scope））、Base URL（公网 https 校验）、spec 原文、解析出的工具清单、已选工具、凭证密文与掩码。
2. 解析（CAP-040）：OpenAPI 3.0/3.1（YAML/JSON，粘贴或上传）每个 path + method 一个工具，名称取 operationId（缺失时 method + path 规范化），参数取 parameters + requestBody 生成 JSON Schema；GraphQL SDL 每个 Query/Mutation 字段一个工具。解析失败返回行号与原因；`$ref` 只允许文档内引用（拒绝外部 URL 引用，防 SSRF）。
3. 工具勾选与测试调用（CAP-041）：选择工具、填写参数、查看响应（经 T-071 执行，写操作在测试时也需二次确认）。
4. 状态：无 spec 保存为草稿；有 spec、测试调用成功且至少选 1 个工具为可用。审批与权益同 T-072。
5. OAuth 2.0 令牌由执行器在调用时获取并缓存（加密，按过期时间刷新）。

## 验收标准

- [ ] 解析器单元测试：Petstore 示例、缺 operationId、外部 `$ref` 被拒、GraphQL Mutation 标记为写操作、错误行号。
- [ ] 测试：非 Base URL 主机的请求被执行器拒绝（同主机约束）。

## 完成记录（2026-11-12，Claude Sonnet 5.5）

**迁移**：`0050_api_plugins`（新表 `api_plugins`，依赖 `0049_flow_connections`，所以本分支叠在 T-075 之上；可降级）。已在一次性 PostgreSQL 库上验证：升级 → `alembic check` 无差异 → 降级 → 再升级。`requirements.txt` 显式加入 `pyyaml>=6,<7`（原先是 langchain 的间接依赖）。

**后端**
- `api_spec.py`：OpenAPI 3.0 / 3.1（YAML / JSON）与 GraphQL SDL 解析。每个 path + method 一个工具，名称取 `operationId`（缺失时 method + 规范化路径，重名自动加后缀）；`parameters` 与 `requestBody` 生成 JSON Schema（请求体作为 `body` 参数）；`$ref` 只允许文档内引用并内联（循环 / 过深截断），外部文件或 URL 引用直接拒绝并给出行号；GraphQL 每个 Query / Mutation 字段一个工具，Mutation 标为写操作，调用时自动生成带变量声明的查询并选取返回对象的标量字段。解析失败返回行号和原因（JSON / YAML 语法错误、缺引用、非 3.x、空 paths 等）。
- `api_plugin_service.py`：认证（无 / API Key 请求头 / Bearer / OAuth 2.0 客户端凭证——令牌在调用时获取，加密缓存，到期前 60 秒刷新）、请求组装（路径参数 URL 编码并拒绝 `.` / `..`，查询 / 请求头 / JSON 体，禁止通过参数设置 `Host`、`Authorization` 等请求头）、经 T-071 的 `safe_request` 调用并**限定为插件自己的主机**（`allowed_host`）、错误映射（401/403 凭据被拒、其他非 2xx、GraphQL `errors`、重定向被拒、网络）；执行器 provider `api:{能力id}:{工具}`、挂载解析器、候选列表（只列可用插件）。
- `api/capabilities.py`：`POST /capabilities/api/parse`（预览解析，不保存）、`POST /capabilities/api`（保存草稿，无规范也可）、`PUT /capabilities/api/{id}`（改规范 / 地址 / 认证会重置测试结果，已可用的降回草稿；`selected_tools` 校验）、`POST /capabilities/api/{id}/test-call`（经执行器真实调用一个已勾选工具；**写操作测试时同样需要 `confirmed: true` 才发请求**，未确认返回 `confirmation_required` 且不发请求；成功的调用即“连接测试”，满足条件后按 CAP-053 提交启用审批）；通用的列表 / 启停 / 复制（凭据随副本，令牌缓存与测试结果不带）/ 删除 / 引用 / 计数对 `api` 类型生效。权益沿用 `external_flows`，权限沿用 `capabilities:*`，每个写操作有审计与 CSRF；密钥加密、响应只有掩码。
- `ToolContext.allow_unavailable`：测试调用可用草稿插件，已停用的不行。`ToolMount.kind` 与 `ToolRuntime` 增加 `api`，已挂载插件勾选的工具会作为工具提供给模型。

**前端**：能力页 API 插件页签启用（列表、状态、卡片）；添加 / 编辑页（`ApiFormPage`）：REST / GraphQL、认证方式四选一、规范粘贴 + 上传 + 解析预览（失败显示行号）、工具勾选（只读 / 写操作标识、“勾选全部只读工具”）、测试调用（参数 JSON、响应展示、写操作二次确认对话）；中英文案。

**验证**：ruff；`tsc -b`、lint、`i18n:check`、`style:check`、`build`；`tests/test_api_spec.py` 20 项（Petstore、缺 operationId、重名、外部 `$ref` 被拒含行号、引用循环、GraphQL Mutation 写标记、JSON / YAML 错误行号、1 MB 上限等）；`tests/test_api_plugins.py` 45 项（含继承的 AssistantTests）：保存 / 解析 / 校验、密钥掩码与加密、测试调用成为可用的条件、写操作二次确认且未确认不发请求、改规范重置、GraphQL 调用与错误、路径 / 查询 / 请求头位置、`..` 被拒、凭据只发往插件主机、重定向与认证失败、Bearer / 无认证不带凭据头 / OAuth 客户端凭证的获取缓存刷新与加密、启停复制删除、审批、挂载与候选、智能体工具列表、跨租户 404、权限 403、CSRF、套餐权益。`mountable_kinds` 已含 `api`。

**是否连过真实第三方 API：没有。** 全部用 `httpx.MockTransport` 验证；真实 TLS 下“连到固定 IP、证书按原域名校验”仍沿用 T-071 的遗留项，需用真实 https 接口验一次。

**与规格的差异 / 遗留**
1. 规格 3 的“填写参数”做成 JSON 文本框，没有按 JSON Schema 生成表单。
2. 查询参数里的数组用逗号拼接（OpenAPI 默认的重复键写法 `explode` 未支持，`safe_request` 只接受字典）；不支持 `application/x-www-form-urlencoded` / multipart 请求体，也不处理 cookie 参数与 `style` / `allOf` 以外的复杂组合（`allOf` / `oneOf` 原样保留在 schema 中）。
3. GraphQL 只做阻塞式查询；返回 union / interface 不生成选择集（需要 fragment，暂不支持）；`subscription` 不提供。
4. 首次解析后**默认不勾选任何工具**（写操作不默认开放），管理员逐个勾选；与 MCP“首次全选”不同。
5. 令牌缓存按插件存一份；多个 Worker 同时过期时可能各取一次令牌，不影响正确性。
6. 健康检查（CAP-032 对 MCP 的定时探测）没有做；API 插件的失败只在调用和测试时体现。

## 评审修正（2026-11-12）

代码评审（`/code-review high`）发现 10 条问题，已全部修复并补回归测试；评审之外还修了 1 条同类问题：

1. 换认证方式（API Key ↔ Bearer、OAuth ↔ 其他）且密钥框留空时，不再沿用旧认证方式下保存的凭据（422 `capability.token_required` / `oauth_secret_required`），留空保持不变只在认证方式没变时生效。
2. `test-call` 补上 `ensure_entitlement`（与其他接口一致），无权益时 403 且不发任何请求。
3. OpenAPI 解析加了展开节点预算（20 万）与工具定义总大小上限（4 MB），并且只展开工具需要的部分（参数、请求体），不再展开 responses / callbacks；循环互引的小规范会在 5 秒内报“展开后过大”。
4. GraphQL `directive` 定义（含 `repeatable`、多个位置）被接受并忽略；不完整的定义仍报错。
5. 前端换规范后工具清单显示新解析出的工具，勾选自动去掉已不存在的工具；保存时未预览则不提交 `selected_tools`，由服务端对账，不会再因旧勾选名 422。
6. OAuth 客户端凭证的令牌在 401 时清掉缓存并重试一次；再次被拒则报 `tool.auth_failed`，不循环。
7. 与参数同名的 `body` 不再被请求体覆盖：请求体参数改名 `request_body`（`binding.body_arg`；旧数据没有 `body_arg` 时按 `body`）。
8. 认证头与模型填写的同名请求头按大小写不敏感比较，模型填的那份被丢弃。
9. `GET /capabilities?kind=api` 只返回工具名与勾选状态，不带 `input_schema`（单个详情才带）。
10. `host_of` / `mask` 复用 `capability_service` 里的实现。
11. **同类问题（评审未报）**：只改 Base URL / Token URL 的主机而不重填凭据，会把已保存的凭据发到新主机（有 `capabilities:edit` 的人可借此套取密钥）。现在换主机必须重新填写凭据（422 `capability.credential_reentry`）；同主机改路径不受影响。T-072 的 MCP 连接有同样的问题，已另行修复（见 `T-072-mcp.md` 的“评审修正”）。

测试：`test_api_spec` 24 项、`test_api_plugins` 56 项（含继承）、`test_flow_connections` 64 项（含继承）。
