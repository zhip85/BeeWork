# T-072 MCP 连接

**版本**：V0.2　**依赖**：T-070、T-071、T-022　**PRD**：3.5.1–3.5.2、3.5.4（CAP-030–032）、5.1 MCP 状态机、5.8 异常事件、SET-071（启用第三方 Skill / MCP）、8.3#15、PLT-081

## 产品决策（按 PRD 暂定执行）

8.3#15：stdio 传输只在私有化/独享部署开放。SaaS 部署中下拉不显示 stdio；私有化开关在 T-124 实现，本任务不实现 stdio。

## 范围

1. `capabilities` 通用表（三类能力共用：类型、名称（同类型唯一）、描述、状态、`row_version`、审计字段）与 `mcp_connections`（传输方式 streamable_http / sse、地址、认证方式、凭证密文与掩码、OAuth 令牌密文与到期时间、已拉取工具清单、已选工具、最近测试结果、健康状态）。
2. 客户端用官方 `mcp` Python SDK，所有出站经 T-071 的安全 HTTP 客户端。
3. 认证：None / Bearer（密码框、掩码、留空不修改）/ OAuth 2.1（授权码 + PKCE，回调 `/api/v1/capabilities/mcp/oauth/callback`，state 绑定租户与用户，令牌加密存储，到期前刷新，“重新授权”）。
4. 测试连接（CAP-030）与工具列表（CAP-031）：首次全选；再次测试保留已选、新工具默认不选、消失的移除。
5. 状态（CAP-032）：最近一次测试成功且至少勾选 1 个工具才可用；健康检查（Worker 每 5 分钟）连续失败标记“连接异常”并通知租户管理员，不自动停用。
6. 新建或启用受“启用第三方 Skill / MCP”审批策略约束（通过前不可用）；套餐权益 `mcp`（专业版及以上，PLT-083 锁定）。
7. 页面：能力列表（CAP-001–008，三个页签共用）与 MCP 创建/编辑页（3.5.2、3.5.4）。

## 验收标准

- [ ] 用一个本地测试 MCP 服务（官方 SDK 示例服务器，Compose 外运行）跑通连接、列工具、调用一次工具；记录在 development-status。
- [ ] 测试：OAuth state 被篡改时拒绝；凭证留空不修改；无权益 403；审批未通过不可挂载。

## 完成记录

**改动文件**
- 后端：`models/capabilities.py`（通用 `capabilities` 表 + `mcp_connections`）+ 迁移 `0043_capabilities_mcp`（可降级）、`tools/mcp_client.py`（MCP 客户端）、`capability_service.py`（凭证与 OAuth、测试与工具对账、`is_ready`、挂载解析器与候选 lister、`McpToolProvider` 注册到 T-071 执行器、健康检查）、`api/capabilities.py`、`schemas/capabilities.py`、`worker/scheduler.py`（每 5 分钟 `capabilities.mcp_health`）、`runtime/executor.py` 导入以注册 provider；审计 `capability.*`（归“智能体”分类）。
- 前端：`pages/capabilities/`（`CapabilitiesPage`：能力列表 CAP-001–008，Skill / MCP / API 三个页签，现在只有 MCP 可用，其余写明“后续版本提供”；`McpFormPage`：创建 / 编辑）、导航“能力目录”接入真实页面（`/capabilities`、`/capabilities/mcp/new`、`/capabilities/mcp/:id`）、`i18n/{zh-CN,en}/capabilities.json`。
- 接口：`GET /capabilities`（`kind`、`q`）、`/capabilities/summary`、`GET /capabilities/{id}`、`/references`、`POST /capabilities/mcp`（连接：保存草稿 + 测试 + 申请启用）、`PUT /capabilities/mcp/{id}`、`POST /capabilities/mcp/{id}/test`、`/oauth/start`、`GET /capabilities/mcp/oauth/callback`、`POST /capabilities/{id}/enable | disable | copy`、`DELETE`。权限沿用 `capabilities:view / edit / publish / delete`。

**规则落实**
- 传输方式只有 Streamable HTTP（SaaS 不开放 stdio，按规格）；地址必须 https，开发开关 `ALLOW_INSECURE_TOOL_URLS` 才允许 http；所有出站经 T-071 的 `safe_request`。
- 测试连接（CAP-030/031）：首次全选；再次测试保留已选、新工具默认不选、消失的移除；改地址 / 认证 / 凭证会作废旧测试结果与工具列表并回到草稿。
- 状态（CAP-032）：最近一次测试成功且至少勾选 1 个工具才可用；新建与启用走“启用第三方 Skill / MCP”审批（策略关闭时直接生效）；没有 MCP 权益返回 403 `plan.feature_locked`（仍可查看）。
- 凭证：Bearer 令牌与 OAuth 客户端密钥、访问 / 刷新令牌加密存储，响应只返回末四位；留空不修改；复制能力带走凭证但不带 OAuth 令牌与测试结果。
- OAuth 2.1：授权码 + PKCE；`state` 为签名 JWT（租户、用户、能力、PKCE verifier，10 分钟有效），回调要求已登录且与签发对象一致；令牌过期前 60 秒内自动刷新。
- 健康检查：连续 3 次失败标记 `error` 并通知租户管理员一次，恢复后清除；**不自动停用**。
- 与智能体：挂载解析器（可用 / 已停用 / 已删除）、挂载候选、`mcp_tool_specs`；执行器里工具名为 `mcp:{能力ID}:{工具}`，只有勾选的工具可见，工具声明 `readOnlyHint` 的免确认，其余写操作要用户确认；服务返回的错误与凭证不会进入模型上下文。

**验证**：ruff；全量 `./scripts/test-postgres.sh` 跑过一轮（861 项，9 项失败：2 项因验证时临时打开的 `ALLOW_INSECURE_TOOL_URLS` 已关闭，7 项是旧测试写死的“只有知识库可挂载 / 定时任务数量”，已更新）。第二轮全量因主机负载过高（load average 40–70）中途停止，改为单独重跑受影响的 6 个模块（`test_worker`、`test_versions`、`test_knowledge_retrieval`、`test_agents`、`test_capability_catalog`、`test_mcp`，共 160 项）全部通过；迁移升降级与 `alembic check` 均通过。前端 `tsc -b`、lint、`i18n:check`、`style:check`、build。`tests/test_mcp.py` 42 项（客户端：JSON / SSE 响应、分页、会话、协议错误码、安全客户端规则；能力：创建 / 校验 / 重名、Bearer、工具对账、版本冲突、审批流转（用第二位管理员批准并由 Worker 生效）、没有其他审批人时仍保存草稿并提示、套餐锁定、启停、复制、删除与引用、列表、OAuth 全流程含 PKCE 校验、篡改 / 过期 / 他人签发的 state 被拒、令牌刷新、健康检查与调度、挂载与执行器、权限、跨租户、CSRF）。
**是否连过真实第三方引擎**：**连过官方 MCP SDK（`mcp` 1.30）的示例服务器**（Streamable HTTP，响应为 SSE 帧）：列出 `add` / `echo` 两个工具、调用得到 `42` 与 `hello 你好`、拿到会话号；经 api 容器内的客户端完成，未经过浏览器 OAuth。**OAuth 只用模拟授权服务验证，未连过真实授权服务器。** 无头 Chrome 走了新建表单与“套餐未包含 / 没有其他审批人”两个提示。

**与规格的差异（可逆）**
- 没有使用官方 `mcp` SDK 的客户端：它是异步的、自己管理连接，无法套用 T-071 的“解析一次并固定已校验 IP”；协议面很小（initialize、tools/list、tools/call），直接在 `safe_request` 上实现。传统 HTTP+SSE 传输（规范已弃用）未提供。
- OAuth 的授权地址、令牌地址、客户端信息由管理员手工填写，未做授权服务器元数据发现与动态客户端注册。

**遗留问题**
- 运行时还没有调用 `mcp_tool_specs` / `invoke_tool`（T-061）；输出护栏要由 T-061 接到 `pii_output`。
- 真实 TLS 下的“固定 IP + 证书按原域名校验”仍只靠 httpx 的 `sni_hostname`，需要连一个真实 https 的 MCP 服务验证一次。
- 删除 / 停用时“被路由规则引用”的确认文案在 T-058；列表页现在只显示引用数。
- 开发库里“小智科技”的套餐快照被临时打开了 `mcp` / `skills` 权益，并留有测试连接（视环境而定）。
