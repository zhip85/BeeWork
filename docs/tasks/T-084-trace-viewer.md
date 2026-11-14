# T-084 调用追踪

**版本**：V0.3　**依赖**：T-054、T-025　**PRD**：3.8.5（OPS-040–042）、OPS-003、NFR-012、PER-011、8.3#13

## 第 0 步：实测（写入本文件“实测记录”）

确认 Langfuse Cloud v2 接口（`GET /api/public/v2/observations` 按 trace 过滤、分页、字段）与**删除 trace 的可用接口**（旧 `/api/public/traces/*` 已 410）。删除接口不可用时，保留期清理只删 BeeWork 侧记录，并在完成记录中写明 Langfuse 侧依赖其项目级数据保留设置。

## 范围

1. 列表（OPS-040）：数据来自 BeeWork 的 `runs` 表（不是 Langfuse），列 Trace ID、助理、延迟、Token、状态（成功 / 错误 / 慢，慢阈值默认 5 秒可配置）；筛选助理、渠道、状态、时间、延迟下限、来源（线上 / 调试 / 试运行 / 评测）；按 Trace ID、会话 ID 精确搜索；按租户与成员范围过滤。
2. 详情（OPS-041）：服务端按 run_id 从 Langfuse v2 读取 observations，组装瀑布图（阶段 span 名称由 T-054 固定）：每个 span 的开始、耗时、状态、输入/输出（已脱敏）、模型与 Token、点数、错误；可跳转会话。Langfuse 不可用时只显示 BeeWork 侧的 run 事件时间线并提示。
3. 保留期（OPS-042，8.3#13 暂按标准 7 / 专业 30 / 企业 90 天）：取套餐 `trace_retention_days` 与租户会话保留期的较小值，Worker 每日清理。
4. 平台管理员不能查看租户追踪内容（PER-011）。

## 验收标准

- [ ] 测试：跨租户 404；Langfuse 客户端用 MockTransport 的契约测试；保留期清理（可注入时钟）。
- [ ] 真实 Langfuse 中一次线上对话能在详情页看到完整瀑布（有 Key 时记录）。

## 需求变更（2026-11-12）

追加路径 A / B、JEV 置信度、能力调用链与按路径成本，见 `T-168`。

## 实测记录（2026-11-14，Langfuse Cloud）

- **读取**：`GET /api/public/v2/observations?traceId=…&fields=core,basic,time,io,usage,model&limit=100`，200。默认只返回核心字段；要输入 / 输出、用量、模型，必须带 `fields`。分页用 `meta.cursor`，下一页传 `cursor`。一次真实对话（路径 A 调叶子智能体）返回 17 个观察：`assistant.run` 根、10 个 `oc.*` 阶段 span、`oc.tool.agent:…` / `oc.leaf.…`、两次 `ChatOpenAI` generation（带 `model`、`usageDetails.input/output`、`input`、`output`；`costDetails` 为空，网关没有定价）。
- **旧接口**：`GET /api/public/traces/{id}` 返回 410；`GET /api/public/v2/traces/{id}` 返回 404。BeeWork 不依赖它们。
- **删除**：`DELETE /api/public/traces`，请求体 `{"traceIds": [...]}`，200（批量）；`DELETE /api/public/traces/{id}` 也可用。不存在的 id 不报错（只用不存在的 id 试过，没有删除任何真实数据）。`POST /api/public/traces/delete` 是 405。所以保留期清理**可以**在 Langfuse 侧真正删除，不需要依赖项目级保留设置。

## 完成记录（2026-11-14，Claude Sonnet 5.5）

分支 `task/T-084-call-tracing`。**无迁移**。

**后端**
- `integrations/langfuse.py`：`fetch_observations(trace_id)`（v2、`fields`、cursor 分页、最多 1000 个）与 `delete_traces(ids)`（批量 DELETE）；失败统一抛 `TracingError`。
- `traces.py`：来源归类（`online` 线上 / `debug` 调试 / `trial` 试运行 / `eval` 评测）、状态（成功 / 错误 / 慢 / 进行中；慢阈值 `TRACE_SLOW_MS`，默认 5000 ms）、`build_spans`（按开始时间排序、相对偏移、耗时、错误、模型、Token、成本；输入输出再做一次固定规则脱敏并截到 4000 字）、`timeline_of`（BeeWork 自己的运行事件）、`load_spans`（Langfuse 不可用 / 未配置 / 已过期时返回原因，不抛错）。
- `api/traces.py`：`GET /api/v1/traces`（筛选：智能体、渠道、状态、来源、时间、延迟下限；`q` 精确匹配 Trace ID / 运行 ID / 会话 ID，不做模糊搜索）、`GET /api/v1/traces/{run_id}`。权限 `traces:view`；范围沿用智能体可见性（`_visible_statement`）；员工门户的私有对话不出现；已删除的智能体不出现；平台管理员没有路由、非本租户成员 403 / 404（PER-011）。
- 保留期（OPS-042）：`traces.retention` 每日任务；保留天数 = min(套餐 `trace_retention_days`，缺省按套餐 标准 7 / 专业 30 / 企业 90；租户会话保留期)。到期的运行先在 Langfuse 删除 trace，成功后清空 `runs.trace_url` 并写审计 `trace.retention_purged`；删除失败则保留链接，第二天重试。运行记录本身随会话保留期清理（`conversations.retention`）。
- 套餐里目前没有 `trace_retention_days` 字段（只有 `log_retention_days`），所以用上面的缺省表；以后产品定了在套餐里加字段即可，代码已优先读它。

**前端**：`pages/traces/TracesPage.tsx`（列表、筛选、分页、右侧详情抽屉：瀑布图含层级缩进、耗时条、错误标红、点开看模型 / Token / 成本 / 输入输出；Langfuse 不可用时显示提示与运行事件时间线；可跳到会话）、侧边栏“调用追踪”入口（`traces:view`）、中英文案（`traces` 命名空间）。

**验证**：ruff；`test_traces` 12 项（筛选组合、时间与精确搜索、分页、跨租户 / 门户私有会话 / 不存在的运行 404、瀑布组装与脱敏、Langfuse 不可用与已过期的回退、MockTransport 契约测试（路径、`fields`、cursor 分页、Basic 认证、401 / 500 / 连接失败、批量删除请求体）、保留期（套餐与会话保留期取小、可注入时钟、Langfuse 删除失败保留链接并次日重试、审计）、每日调度包含该任务）；既有 `test_worker` 两项硬编码了每日任务清单，已加入 `traces.retention`；全量后端 1390 项（其余 1388 项通过，修正这两项后 `test_worker` + `test_traces` 27 项通过，未再整体重跑）；前端 tsc / lint（新增文件无警告）/ i18n:check / style:check / build。**真实 Langfuse**：用开发库里一次线上对话的 trace 读取并组装，17 个 span 的层级、耗时、模型、Token 与 Langfuse 一致（上面实测记录）；删除接口只用不存在的 id 试过。

**未完成 / 遗留**
1. 页面未目视验收，也没有在真实栈里点过一次“列表 → 详情”（接口与组装用真实 trace 验证过，页面只过了类型 / lint / 构建）。
2. 验收项“真实 Langfuse 中一次线上对话能在详情页看到完整瀑布”：服务层已用真实 trace 验证，经 HTTP 接口与页面的完整链路待目视。
3. `costDetails` 为空：Langfuse 里没有成本，页面的“成本”只在有值时显示；“点数”（按路径 / 能力聚合）属于 T-168。
4. 列表没有路径 A / B、转 B 原因、失败的能力等筛选，Langfuse span 的 `l2.*` / `path.a.*` 约定也未加，均在 T-168。
5. 渠道筛选的选项是固定的几个值（website / api / wecom / h5 / wechat），后续渠道增加时需同步。
6. 保留期清理每个租户每天最多处理 500 条，积压多时要几天清完。

