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
