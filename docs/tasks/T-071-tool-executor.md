# T-071 受控工具执行器（出口网关）

**版本**：V0.2　**依赖**：T-001、T-054　**PRD**：CAP-042、CAP-054、NFR-009、technical-plan 第 2 节“工具执行器”、5.5 第 8、10 步

## 目标

所有工具调用（MCP、API 插件、作为工具的 Workflow、Skill 资源加载）只经过这一个执行器，统一做授权、校验、出站安全、审计与追踪。

## 设计

- `ToolSpec`：工具全名、来源（mcp / api / workflow / skill）、参数 JSON Schema、是否写操作、所属能力 ID。
- `invoke_tool(ctx, tool_name, args)`，`ctx` 含 tenant、assistant/agent、run、调用深度、终端用户。检查顺序：能力属于本租户且状态可用 → 在当前助理/Agent 的挂载与勾选工具内 → 参数按 Schema 校验 → 写操作确认 → 执行 → 输出护栏。
- 出站 HTTP（MCP HTTP/SSE、API 插件）：统一 `safe_http_client`：每次请求解析主机并用 T-001 的规则拒绝内网地址，**连接时固定到已校验的 IP**（消除 T-001 遗留的 DNS rebinding 窗口）；禁止自动跟随重定向（或逐跳重新校验）；API 插件只允许 Base URL 同主机；超时 30 秒；响应体 ≤ 1 MB。开发调试开关 `ALLOW_INSECURE_TOOL_URLS`（默认 false，仅本地开发可开）允许 http 与本机地址。
- 写操作（POST/PUT/PATCH/DELETE、GraphQL Mutation）默认“需确认”（CAP-042）：运行中用 LangGraph `interrupt` 向终端用户发确认请求，确认后继续；拒绝则返回“用户取消”。写操作带幂等键（run_id + 调用序号）。
- 凭证只在执行器内部解密使用，不进入模型上下文、trace、日志。
- 记录：`tool_invocations` 表（租户、run、工具、耗时、状态、错误码、请求/响应大小，不存正文）+ Langfuse span（输入输出经 PII 脱敏）。工具返回内容在交回模型前经过管线的 `pii_output` 钩子（CAP-054；T-054 提供钩子，T-055 完成后规则自动生效，两者都完成前不得对外发布含工具的助理）。

## 验收标准

- [ ] 安全测试：解析到 10.x / 169.254.169.254 / ::1 的主机被拒；首次解析公网、连接时解析内网（rebinding 模拟）被拒；跨主机重定向被拒；超大响应被截断报错；未挂载工具被拒。
- [ ] 写操作确认的中断与恢复测试（假模型）。
- [ ] trace 与日志中无凭证（测试中用哨兵字符串检查）。

## 需求变更（2026-11-10）

- 外部流程连接（T-075，Dify）与企业微信接口（T-095）的出站调用同样经本执行器，统一做允许域名、超时、重试、限流与敏感字段脱敏（演进 PRD 7.5、8.3）。

## 完成记录

**改动文件**
- `tools/schema.py`（工具参数的 JSON Schema 子集校验，不新增依赖）、`tools/http.py`（`safe_request`：只在发起请求时解析一次域名，**所有**解析结果都必须是公网地址，再把连接固定到已校验的地址，TLS 服务器名与 `Host` 保持原域名；不跟随重定向；30 秒超时；响应超过 1 MB 报错而不是静默截断；API 插件可用 `allowed_host` 限定同主机；开发开关 `ALLOW_INSECURE_TOOL_URLS` 允许 http 与本机地址，默认关）、`tools/executor.py`（`ToolSpec`、`ToolContext`、`ToolProvider` 协议与 `PROVIDERS` 注册表、`invoke_tool`）、`models/tools.py` + 迁移 `0039_tool_invocations`（可降级）、`config.py` 新增开关。
- `invoke_tool` 检查顺序：工具存在且能力属于本租户 → 在智能体挂载并勾选的工具内 → 调用深度 ≤ 5 → 参数符合 Schema → 写操作确认 → 执行 → 输出护栏。拒绝与失败以 `ToolResult(status="error")` 返回给模型；只有“写操作尚未确认”会抛 `ToolConfirmationRequired`（带 `call_index` 与幂等键 `run_id:序号`），图在 T-061 / T-063 把它转成 LangGraph `interrupt`，恢复时带 `confirmed` 与同一 `call_index` 再调；拒绝返回“用户取消”；同一幂等键已成功执行过则不重复执行。
- 凭证：provider 解密后用 `ctx.register_secret` 登记，返回内容、错误信息都会被替换为 `***`；provider 抛出的未知异常只记一条不含异常文本的日志，返回“工具执行失败”。`tool_invocations` 只存元数据（租户、run、智能体、工具、来源、是否写、状态、错误码、耗时、请求/响应字节数、幂等键），不存参数、结果和凭证。
- 测试：`tests/test_tool_executor.py` 26 项——内网 / 回环 / 链路本地 / IPv4 映射 / 未指定地址（按域名和按字面量）被拒；混有内网地址的域名整体被拒；DNS rebinding 模拟（只解析一次、连接固定在首次校验的公网地址）；重定向、超大响应（含声明的 Content-Length）、超时、同主机限制、多地址回退；一个不用 mock 的真实本地套接字测试（验证连到固定地址、`Host` 头与查询串）；未挂载 / 未知 / 跨租户 / 深度超限 / 参数错误；写操作暂停 → 确认恢复只执行一次、拒绝取消；哨兵字符串在返回内容、日志、记录里都不出现；输出护栏与结果长度上限。

**验证**：ruff；`./scripts/test-postgres.sh` 全量（迁移升降级 + `alembic check` 无差异）。写测试时发现并修掉一个真实问题：固定地址重建 URL 时丢了原 URL 的查询串。
**是否连过真实第三方引擎**：不涉及（本任务没有具体工具来源）。

**遗留问题**
- 真实 TLS 握手下“连到固定 IP、证书按原域名校验”只靠 httpx 的 `sni_hostname` 扩展保证，本地没有证书所以没做端到端验证；T-072 / T-075 接真实服务时需要验一次。
- 目前没有任何 provider 注册（MCP T-072、API 插件 T-073、Skill T-074、流程连接 T-075 各自注册），运行时也还没有调用 `invoke_tool`（T-061）；`ctx.guard_output` 要由 T-061 接到管线的 `pii_output` 钩子，两者完成前不得对外发布含工具的智能体。
- 响应只做了大小限制，没有做内容类型限制；限流与重试策略留给各 provider。
- Langfuse span 只有名称，输入输出的 PII 脱敏随 T-061 接入。

## 需求变更（2026-11-12）

超时、重试、退避、熔断与审计的统一策略见 `T-165`。
