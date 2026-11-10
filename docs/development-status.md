# BeeWork 开发进度与交接记录

> 本文件是**唯一**的开发状态记录：每完成一个垂直功能模块，在这里更新状态、验证证据、未完成项和下一步。[`technical-plan.md`](./technical-plan.md) 只放路线图、依赖和验收标准，不重复记录进度。不要仅依据原型页面判断功能已完成。
>
> 状态核对日期：2026-10-22

## 1. 继续开发前先读什么

1. [`BeeWork-prototype.html`](./BeeWork-prototype.html)：功能范围、页面、导航与交互的主依据。
2. [`BeeWork-PRD.md`](./BeeWork-PRD.md)：业务规则、字段、状态、权限和验收；PRD 漏写不代表原型功能不做。
3. [`technical-plan.md`](./technical-plan.md)：架构（含第三方引擎接入方式）、MVP 切线、阶段依赖、阻塞决策。
4. [`../README.md`](../README.md)：本地启动、账号、接口、测试命令。
5. 本文件：当前实现状态与交接注意事项。

## 2. 当前总体判断

项目已有可运行的 SaaS 底座（租户、组织、角色、审计、租户生命周期、模型目录与环境配置），并接入了 AI 网关 LiteLLM；**还没有形成 AI 助理产品闭环**（助理构建、审批、发布、知识检索、LangGraph 真实运行均未开始）。最小 LangGraph 示例只用于验证依赖。没有可信的完成百分比。

| 阶段 | 范围 | 状态 | 说明 |
|---|---|---|---|
| 0 | 工程底座 | 大部分完成 | Compose、迁移、按领域拆分的前后端代码、ruff/oxlint、SQLite + PostgreSQL 测试、CI 已就绪；Worker/队列已实现（T-002）。 |
| 1 | 租户、组织、角色权限与审计 | 收尾中 | 缺：i18n 框架、登录失败锁定、邀请邮件、生产清理调度/告警、外部存证。 |
| 2 | 平台定义、套餐权益与模型配置 | 进行中 | 已完成：模型目录、环境配置（非企业版仅生产）、租户生命周期 7 态 + 双人复核、LiteLLM 同步与连通测试。缺：套餐版本/权益/配额、BYOK、跨环境发布、真实供应商调用验收（缺供应商 Key）。 |
| 3 | 助理构建、审批、版本与最小运行闭环 | 未开始 | 前置：Worker/队列、首发模型供应商。 |
| 4 | 知识库（Dify） | 已完成（有条件） | T-030/T-031/T-033 已完成：知识库、文档、挂载与运行时检索，Dify 已用真实环境验证；FAQ（T-032）未做。 |
| 5 | OC、能力目录与智能体运行 | 未开始 | 前置：平台 PII/术语/基线最小版。 |
| 6 | 渠道、Widget 与端到端发布 | 未开始 | |
| 7 | 运营、审批中心、评测与可观测 | 未开始 | Langfuse 仅有连通性检查。 |
| 8 | 完整租户设置与平台运营后台 | 部分开始 | 平台总览、租户生命周期、平台审计、集成状态已有首版。 |
| 9 | 生产交付 | 未开始 | |

### PRD / 原型功能覆盖

状态含义：**已实现**＝服务端持久化、授权和关键行为有验证；**部分实现**＝有可运行切片但有缺口；**未开始**＝即使原型页面或菜单存在也算未开始。

| PRD / 原型模块 | 状态 | 主要缺口 |
|---|---|---|
| 全局框架、身份、租户上下文（2、6） | 部分实现 | 中英双语、全局搜索、通知中心、个人资料；业务资源授权随模块落地；平台角色分权。 |
| 工作台（3.1） | 未开始 | |
| 助理（3.2） | 未开始 | |
| 智能体（3.3） | 未开始 | |
| 知识（3.4） | 部分 | 知识库、文档、挂载与检索已做（Dify）；FAQ 未做。 |
| 能力（3.5） | 未开始 | |
| 编排 OC（3.6） | 未开始 | |
| 渠道（3.7） | 未开始 | |
| 运营（3.8） | 未开始 | |
| 审批中心（3.9） | 未开始 | |
| 设置（3.10） | 部分实现 | 已有：成员、角色、组织、审计日志、模型与环境（含网关同步）。缺：企业资料、SSO、BYOK、规则/术语、合规与审批、订阅用量、API/Webhook、通知。 |
| 案例数据（3.11） | 未开始 | |
| 平台总览与租户（3.12） | 部分实现 | 已有：总览、创建、列表、生命周期 7 态与双人复核。缺：编辑/详情页、配额、自动到期流转、真实健康度。 |
| 运维工单与操作审计（3.13） | 部分实现 | 审计已有；工单未开始。 |
| 套餐与模型目录（3.14） | 部分实现 | 模型目录 + LiteLLM 同步已有；套餐权益未开始。 |
| 渠道类型、Widget 模板（3.15） | 未开始 | |
| 行业术语、PII、安全基线（3.16） | 未开始 | |
| 数据字典 / 状态机 / 权限矩阵（4–6） | 部分实现 | 租户状态机已实现；其余随业务模块。 |
| 非功能需求（7） | 未开始 | |

## 3. 已实现能力与验证证据

### 工程底座

- 代码结构：后端路由按领域拆分在 `backend/src/beework/api/`（auth、audit、members、roles、organizations、platform_tenants、platform_models、tenant_models、integrations）；前端按页面拆分在 `frontend/src/pages/`，共享类型/常量/API 在 `types.ts`、`constants.ts`、`api.ts`。
- 新领域代码组织：`models/` 与 `schemas/` 已改为包（原文件内容在 `__init__.py`，导入路径不变），新领域放 `models/<domain>.py`、`schemas/<domain>.py`，减少并行开发冲突。
- 静态检查：后端 `ruff`（`backend/ruff.toml`），前端 `tsc -b` + `oxlint`，均为 0 告警。
- 测试：`docker compose exec api python -m unittest discover -s /app/tests`（SQLite）；`./scripts/test-postgres.sh` 在一次性库 `beework_test` 上做迁移 upgrade → check → downgrade base → upgrade，再跑全量（含 PostgreSQL 专用测试）。
- CI：`.github/workflows/ci.yml`（后端 lint + 迁移升降级 + 全量测试含 PostgreSQL；前端类型检查、lint、构建）。尚未在 GitHub 上实际运行过。

### 身份、组织、角色与审计（阶段 1）

- 登录/会话/CSRF、平台与企业账号分离、组织树、成员邀请与生命周期、席位、五类内置角色与自定义角色、审计查询/导出/哈希链/365 天保留，详见 README 接口列表。
- 权限：新增 `models` 模块（模型与环境 / BYOK）。**审计员不再能读取模型配置**（PRD 6.1）；迁移 0011 把已有自定义角色的 `settings` 权限复制到 `models`，保持原有行为。
- 并发：租户行锁改为 `FOR NO KEY UPDATE`。原 `FOR UPDATE` 会与写审计记录时外键所需的 `KEY SHARE` 锁互相等待，在登录与邀请并发时可能死锁；`test_tenant_row_lock_does_not_block_tenant_audit_writes` 可稳定复现修复前的问题。

### 租户生命周期与双人复核（PRD 3.12.6、PLT-052、PER-004）

- 7 种状态与 PRD 状态机一致；停用、进入待删除、永久删除必须填写原因，生成待复核操作，另一名平台管理员通过后才执行；发起人不能复核自己；72 小时未处理自动驳回。
- 访问控制：试用/正式/宽限＝正常；只读＝所有成员仅能查看与导出；停用＝仅企业管理员可登录、查看与导出；待删除/已删除＝不可登录，并撤销会话。
- 平台对租户的操作同时写入平台审计和该租户审计（标注 `operator=platform`）。
- **未完成**：试用到期/宽限 7 天/待删除 30 天的自动流转（需 Worker）；永久删除只改状态，**不清除数据、不出具删除证明**；代管登录。

### 模型目录、环境配置与 AI 网关（阶段 2）

- 环境统一为开发/预发/生产（`development/staging/production`）；迁移 0011 把旧值 `testing` 改为 `staging`。非企业版只有生产环境（RUL-004），接口返回 `available_environments`。
- LiteLLM 1.106.0 作为 Compose 服务运行（`docker.litellm.ai/berriai/litellm`，按 digest 固定；自身数据在 PostgreSQL 的 `litellm` 库）。
- 平台模型保存后同步为 LiteLLM deployment（别名 `bw-{模型ID}`，停用则删除）；同步结果记录在 `gateway_sync_status / gateway_error`，可重试。
- 租户环境保存后在 LiteLLM 中建立/更新 team（只放行该环境启用的模型；无模型时放入不可匹配的别名，避免 LiteLLM 把空列表当作“全部模型”）并签发虚拟 Key（加密存于 BeeWork）。
- 连通性测试：“链路检查”用网关凭证请求 `/v1/models`，验证 BeeWork → 网关 → 租户 Key → 模型权限，不调用供应商；“真实调用”需要有效的供应商 Key。
- 平台总览显示 LiteLLM / Dify（及可选的 RAGFlow）/ Langfuse 的连接状态。
- **未完成**：真实供应商调用验收（缺 Key）；LiteLLM 花费数据汇总为点数；BYOK；跨环境发布与审批；套餐权益与配额。

### 知识引擎（Dify）与 Langfuse

- `backend/src/beework/integrations/`：`knowledge_engine.py`（统一接口）、`dify.py`（默认）、`ragflow.py`（可选）——数据集、文档上传、解析状态、删除、检索；`langfuse.py`：连通性检查。
- **知识引擎决定（2026-11-09）：依赖知识引擎的任务一律使用 Dify**（Dify Cloud 已在真实环境验证上传、索引、检索与删除）。`KNOWLEDGE_ENGINE` 默认 `dify`；RAGFlow 客户端与假服务保留为可选实现，**不再验证**，已有的 RAGFlow 知识库仍按其引擎访问。
- Langfuse：**已对接 Langfuse Cloud**（`cloud.langfuse.com`，密钥在本地 `.env`，不入库）。平台集成状态显示已连接；用 OpenTelemetry 接口写入测试 span，16 秒后可通过 `GET /api/public/v2/observations` 读到。
- **Langfuse Cloud 旧接口将于 2026-11-16 下线**：`POST /api/public/ingestion` 只剩 score 事件，`GET /api/public/traces/*` 已返回 410。规范已要求只用 SDK ≥3（OpenTelemetry）写入、v2 接口读取，LiteLLM 侧用 `langfuse_otel` 回调。

### 本轮验证记录（2026-10-22）

- SQLite 套件：33 项通过（4 项 PostgreSQL 专用测试跳过）。
- `./scripts/test-postgres.sh`：迁移 0001→0012 升级、`alembic check` 无差异、降级到 base 再升级通过；全量测试连续多次通过（含并发审计链、触发器、并发邀请席位、锁顺序测试）。
- 开发库已升级到 `0012_gateway_sync`，`alembic check` 无差异；开发引导已创建 `reviewer@beework.dev`。
- 前端 `tsc -b`、`oxlint`（0 告警）、`vite build` 通过。
- **LiteLLM 真实实例验证通过**（本地镜像 1.106.0）：`scripts/check_gateway_e2e.py` 全部通过——平台模型同步为 deployment、平台凭证可见该模型、租户环境同步为 team + 虚拟 Key、租户 Key 只能访问已启用模型、模型停用后租户 Key 失去访问。
- 验证中发现并修复：① 该版本 LiteLLM 代理不接受客户端传入的 `mock_response`（会真实请求供应商），“链路检查”改为 `/v1/models` 权限校验；② 模型停用后租户 team 仍保留其别名，现已在模型变更时重新同步所有使用它的租户环境。
- 尚未用真实供应商 Key 做过“真实调用”。

### API 公共约定（T-001，已完成）

- `api/conventions.py`：`ApiError` 统一错误格式、`Pagination`/`Page[T]`/`paginate`、`VersionedMixin` + `check_row_version`（409）、通用列 mixin、`require_platform_admin`、名称校验、`validate_public_https_url`（SSRF）、`ensure_resource_in_scope`/`scope_filter`。平台接口已全部改用它；旧 `HTTPException` 兼容（补 `code: "error"`）。
- 验证：ruff、`test-postgres.sh`（42 项）、前端检查、网关 e2e 均通过。已知限制：422 校验错误仍无 `code`。

### Worker、任务队列与运行事件（T-002，已完成）

- PostgreSQL 任务表 + `python -m beework.worker`（Compose `worker` 服务，已取代 `audit-retention`）：`FOR UPDATE SKIP LOCKED` 取任务、指数退避、`dead`、幂等键、锁超时回收、每日审计清理定时任务；平台后台“任务”页可查看与重试。
- 运行事件：持久化到 `run_events` 并经 Valkey 唤醒订阅者，SSE 接口按租户隔离，先订阅后读历史，无空洞。
- 验证：ruff、迁移升降级 + `alembic check`、62 项测试（含 PostgreSQL 并发与真实 Valkey）、前端检查、Worker 进程端到端均通过。
- 已知限制：事件接口待 T-025 改为按 `runs` 表校验；锁超时 15 分钟；浏览器需 `fetch` 流式读取（不能用 `EventSource`）。

### 前端中英双语框架（T-003，已验收）

- `i18next` + `react-i18next`（`i18next@26.5.11`、`react-i18next@17.1.9`，已锁定）；字典 `frontend/src/i18n/{zh-CN,en}/*.json`（`common`、`nav`、`auth`、`members`、`platform`、`settings`、`audit`、`enums`），`i18n/format.ts` 提供相对时间、租户时区时间、`18.2k`/`2.48M`、百分比格式化；`npm run i18n:check` 已接入 CI。
- 语言来源：`users.locale`（迁移 0014，`PATCH /auth/me/preferences`，写审计 `auth.preferences_updated`）→ `localStorage` → `zh-CN`；顶栏、平台后台顶栏、登录页、邀请页、无权限页均有 EN / 中 切换。
- 已迁移：应用外壳、平台后台外壳、登录/邀请/无租户页、无权访问占位页，全部枚举（`constants.ts` 已删除，改用 `enumLabel`/`enumOptions`）。
- 已知限制：`members`、`settings`、`audit` 命名空间已建但为空，这些页面及 Dashboard、`CreateModal` 正文仍是中文，需分批迁移；后端错误信息暂不翻译。

### 套餐、订阅快照与配额服务（T-010a，已验收）

- 表 `plans` / `plan_versions` / `tenant_subscriptions`（迁移 0015，三套餐 v1 种子，现有租户按 `tenants.plan` + `tenants.seat_limit` 回填快照；`tenants.plan`/`seat_limit` 列保留为兼容列）。
- `beework/entitlements.py`：`get_subscription`（缺快照时懒创建）、`has_entitlement`/`ensure_entitlement`/`require_entitlement`（403 `plan.feature_locked`）、`check_quota`/`enforce_quota`（409 `plan.quota_exceeded`）、用量计数器注册表；席位逻辑（邀请、重发、接受、恢复成员）已改为读订阅快照；平台创建租户同时写订阅；`GET /api/v1/tenant/subscription`（需 `settings:view`）。
- 已知限制：只有 `seats` 注册了用量计数器，**T-020 必须注册 `assistants` 计数器**（未注册的配额调用 `check_quota` 会抛 `ValueError`，不会静默当作 0）；`environments_for_plan` 与模型可用性仍读 `tenants.plan`；平台套餐页、发布新版本、应用订阅变更、订阅与用量页、RUL-013 属 T-010 剩余部分，未做。

### 助理领域模型、列表与创建向导（T-020，已验收）

- 表 `assistants`、`assistant_visible_organizations`，`tenant_roles.scope_assistant_ids`（迁移 0016）；`draft_config` 由 `AssistantDraftConfig`（`dsl_version: 1`）定义，T-025 编译器按它读取。
- 接口：`GET/POST /assistants`、`GET/PUT /assistants/{id}`（`row_version`）、`POST …/copy|disable|enable`、`DELETE …`、`GET /assistants/summary`、`GET /assistants/model-options`。创建/复制调用 `enforce_quota("assistants")`，并在 `api/assistants.py` 注册了 `assistants` 用量计数器；名称租户内不区分大小写唯一（软删除后释放）；所有写操作写审计（分类“助手”）。
- 可见范围：组织范围角色可见本组织子树 + `all` + 指定了其组织的助理，但只能修改本组织子树内的助理；助理范围角色（SET-022）只看/改指定助理、不能新建，且只能配置工作台与助理权限，删除助理时自动从角色范围移除。
- 前端：助理列表页（统计条含配额进度、搜索/状态/组织/排序、卡片与列表视图、更多菜单、自定义确认弹窗）、4 步创建/编辑向导（保存草稿、未保存确认、配额硬限制提示），角色编辑器增加“指定助理”范围；`assistants` 命名空间中英文齐全；侧栏“助理”不再显示写死的数字。
- 已知限制：模板、驱动方式、能力挂载、AI 生成建议均为“即将开放”（后端对非空挂载/驱动返回 422）；模型选项固定取 `production` 环境（T-006 做环境切换后再改）；卡片/列表上的调用量、成功率、渠道数、月会话量待 T-026/T-040；“对话历史/编排/版本/测试/API 对接”菜单项待对应任务；删除阻断（渠道绑定）只有 `DELETE_BLOCKERS` 注册点，T-040 接入。

### LangGraph 运行时 v1（T-025，已验收，mock 模型 + 检索桩）

- 表 `runs`（迁移 0017）；`runtime/` 包：`dsl.py`（`AssistantDSLv1`）、`compiler.py`（固定管线图、节点注册表）、`llm.py`（`ChatOpenAI` → LiteLLM + 租户环境虚拟 Key）、`retrieval.py`（`Retriever` 协议 + `StubRetriever`）、`executor.py`、`tracing.py`（Langfuse SDK v3）、`jobs.py`（Worker 处理器 `runtime.execute`）。
- 接口：`POST /assistants/{id}/runs`（调试，读取草稿并冻结快照）→ 202 + `run_id`；`GET /runs/{id}`；`POST /runs/{id}/cancel`；`GET /runs/{id}/events`（SSE，改为按 `runs` 校验）。准入：租户状态、助理状态、环境网关同步、模型仍启用；点数配额待 T-011（暂跳过）。
- 取消、超时、模型/网关错误都有终止事件与可见状态；错误信息只给固定中文文案，不回显网关原文。
- **验证用的是 `mock_response` 模型，不是真实供应商**（无供应商 Key）：`scripts/check_runtime_e2e.py` 把一个临时目录模型在 LiteLLM 里替换成带 `mock_response` 的部署，真实走完 API → Worker → LangGraph → LiteLLM → SSE，并在 Langfuse Cloud 读到了 `assistant.run` + `ChatOpenAI` 两个 observation（metadata 含 tenant / assistant / run）。
- 知识检索：T-025 时只有桩实现；已由 T-033 接入真实知识引擎（见下）。
- 已知限制：见任务文件“遗留问题”。

### 会话与消息、保留期与脱敏（T-026，已验收）

- 表 `end_users`、`conversations`、`messages`、`tenant_compliance_policies`（迁移 0018）；`beework/conversations.py`（会话/消息服务、历史窗口、30 分钟无消息结束、按保留期清理）、`compliance.py`、`pii.py`（手机号/邮箱/身份证号/银行卡号基础脱敏）。
- 调试运行 `POST /assistants/{id}/runs` 现在可带 `conversation_id` 续聊（只有创建者、同一助理可续；上一条未结束返回 409 `conversation.busy`），历史窗口默认 20 条（DSL `limits.history_messages`），助理回复入库并累计 turns/tokens。
- 合规设置：`GET/PUT /tenant/compliance`（保留期 30/90/180/365 且不超过套餐 `log_retention_days`、日志脱敏、允许导出；`row_version` 乐观锁；审计 `compliance.updated`），前端“设置 → 数据与隐私”页。
- 脱敏范围：消息入库前脱敏；运行结束后 `runs.input/output` 与 `done` 事件中的文本脱敏并删除 `token` 事件（实时订阅者已收到原文，重放只看到脱敏后的 `done`，事件 `seq` 因此可能不连续）；Langfuse 通过 `mask` 函数在上传前脱敏。
- Worker 定时任务：`conversations.end_inactive`（每 5 分钟窗口）与 `conversations.retention`（每日，每租户写一条汇总审计）；会话结束触发 `CONVERSATION_ENDED_HOOKS`（T-056a 注册记忆抽取）。
- 前端：助理卡片菜单“对话历史”→ `/assistants/:id/conversations`（只读列表 + 抽屉详情）。助理详情页（T-021）建成后应把它收进“会话记录”页签。
- 已知限制：见任务文件“遗留问题”。

### 运行管线骨架（T-054a，已验收）

- 运行图改为 PRD 5.5 的固定十阶段管线 `ingress → admission → pii_input → memory → glossary_rewrite → route → execute → components → pii_output → egress`（`runtime/compiler.py`）。阶段由 `STAGE_FACTORIES` 注册，未注册的阶段直通；后续任务只用 `@register_stage("…")` 填自己的阶段。已实现：`ingress`（输入长度/空检查）、`route`（暂固定到助理自身）、`execute`（知识检索 → 模型）、`egress`；其余直通。
- 每个阶段：`step` 事件（started/completed/failed + 耗时）、Langfuse span `oc.<阶段>`（固定名称，供 T-084 瀑布图）、耗时计入 `runs.stage_ms`；`runs.pipeline_ms` = 各阶段耗时 − 模型/检索调用耗时（NFR-001 指标）；某阶段抛错时 run 失败，`runs.error_stage` 与 `error` 事件的 `stage` 带阶段名。
- `draft_config.oc`：`runtime/oc.py` 的 `OCConfig`（`oc_schema_version: 1`，六个阶段各一个子对象，默认值齐全、未知键 422），随助理 API 校验，并冻结进每次 run 的 DSL。记忆部分已按 PRD 3.6.4 定义字段（短期窗口轮数/Token/类型白名单，长期 4 个文件的 Top-K/保留期/开关）供 T-056a 使用。
- `memory` 阶段是挂点：它写入 `state["memory"]`，`execute` 会把它们放进系统提示词的“记忆”段。
- 验证：真实栈 e2e 在 Langfuse 读到 12 个 observation（`assistant.run`、10 个 `oc.*`、`ChatOpenAI`），管线自身耗时约 4–40 ms（mock 模型；模型本身约 400 ms+）。
- 已知限制：见任务文件“遗留问题”。

### OC 配置框架（T-054，已验收（有条件）；运行管线骨架见 T-054a）

- 页面：`/assistants/:id/oc[/:stage]`——页头、返回、保存草稿、提交审批、助理选择器（切换/离开前对未保存修改确认）、编排总览 + 4 组 6 个阶段导航（URL 直达，默认 PII 规则）、权限只读；助理详情“编排”页签显示六步条并跳转对应阶段。各阶段编辑页由 T-055–T-059 通过 `STAGE_PANELS` / `register_summary` 接入，当前显示“即将开放”。
- 接口：`GET|PUT /assistants/{id}/oc`、`POST /assistants/{id}/oc/submit`、`GET /oc/assistants`。保存只写 `draft_config.oc`；提交走审批事件 `rule`（助理规则变更），生效的新版本是“当前线上快照 + 只替换 OC”，不带上提示词/模型等未审批草稿，也不覆盖审批期间的其他发布；版本类型 `oc_change`。
- 验证：ruff、迁移升降级 + `alembic check`、全量 524 项、前端检查通过。**未做浏览器目视验收；六个阶段的编辑页尚未实现，页面上暂时无法改动 OC。**

### 长期记忆 v1（T-056a，已验收，检索方式待确认）

- 表 `end_user_memories`（迁移 0020；`end_users` 增加 `(id, tenant_id)` 唯一约束以建复合外键）：UserProfile（`assistant_id` 为空，同租户各助理共享）、UserMemory 与 Session 摘要（助理私有）；内容一律先经 PII 规则脱敏再写入。
- 写入：会话结束（T-026 的 30 分钟判定）触发钩子，入队幂等的 `memory.extract`（键含会话与结束时间）；Worker 经 LiteLLM 用该会话所用模型抽取，按 OC 设置（类型白名单、UserProfile 写入开关、各文件开关与保留期）写入；重复抽取会替换自己上次的输出，不产生重复。抽取失败（网关不可用、输出不是 JSON）抛错，由队列退避重试。
- 读取：`memory` 阶段（`runtime/memory_stage.py`）按 优先级 UserProfile → UserMemory → Session，各取 Top-K（OC 中的值）注入系统提示词“记忆”段；`MemoryRetriever` 接口 + `KeywordMemoryRetriever`（关键词/中文双字相关度 × 30 天半衰期的时间衰减）。按租户 + 终端用户隔离，UserMemory/Session 不跨助理，已过期条目不返回。
- 服务函数 `delete_end_user_memories`（供 T-080/T-086 调用，写审计 `memory.user_deleted`）与每日 `memory.retention` 清理。
- 顺带修复一个线程安全问题：图节点跑在 LangGraph 线程池里，原先取消检查会和主线程共用同一个 Session；现在节点内一律用自己的短会话。
- 已知限制：见任务文件“遗留问题”。

### 助理发布与不可变版本（T-024a，已验收）

- 表 `assistant_versions`（迁移 0021）：`snapshot` 含完整草稿配置、基础信息和冻结的运行时 DSL；ORM 层禁止 UPDATE，PostgreSQL 触发器 `trg_assistant_versions_immutable` 拒绝 UPDATE（DELETE 保留，便于租户/助理清除时级联）。版本号 vX.Y（首次 1.0，之后 1.1、1.2…）。
- 接口：`GET /assistants/{id}/publish-preview`、`POST /assistants/{id}/publish`、`GET …/versions`、`GET …/versions/{vid}`、`GET …/versions/compare?from=&to=`（版本 ID 或 `draft`；提示词给行级 diff）、`POST …/versions/{vid}/restore`（覆盖草稿，带 `row_version`，不上线）、`POST …/versions/{vid}/rollback`（用历史快照生成新版本并上线，草稿不变）。发布/回滚写审计 `assistant.published` / `assistant.rolled_back`。
- 运行时：`POST /assistants/{id}/runs` 新增 `target: "live"`，执行线上版本的冻结快照（`source=test`，`runs.version_id` 记录版本）；草稿运行不受影响，发布后继续编辑草稿也不会改变线上行为。
- 发布前检查（最小版）：名称/简介、模型仍启用、环境网关已同步；草稿与线上无差异时拒绝。完整“配置检查”属 T-021。
- 前端：助理卡片菜单新增“发布”（预览变更与阻断项、填写发布说明）与“版本管理”（时间线、查看快照、恢复、回滚、对比任意两个版本/草稿）。
- 已知限制：见任务文件“遗留问题”。

### 助理详情与配置检查（T-021，已验收）

- 详情页 `/assistants/:id`：页头（状态、版本、发布更新、更多菜单）、8 个页签；“概览”（配置检查、配置路径、基础配置、完整提示词弹窗）与“能力”（挂载分组列表，失效红色）为真实数据；“版本”“会话记录”链接到已有页面；其余页签“即将开放”。
- 接口 `GET /assistants/{id}/checks`、`GET /assistants/{id}/mounts`；发布预览新增 `warnings`。配置检查规则见任务文件完成记录。
- 挂载解析走 `capabilities.MOUNT_RESOLVERS` 注册表，目前无已注册类型，挂载写入仍不可用。
- 验证：ruff、`test-postgres.sh`（177 项）、前端 tsc/lint/i18n:check/build 通过；未目视检查页面。

### 审批引擎与审批中心（T-022，已验收）

- 表 `approval_policies`（每租户 7 个事件，懒创建默认值）、`approval_requests`（含部分唯一索引：同对象同类型仅一个待审批单）、`approval_comments`；`tenant_roles.approver_tags`（安全负责人 / 知识负责人标记）。迁移 `0022_approvals`。
- 引擎 `beework/approvals.py`：`submit_change`、`decide`、`withdraw`、`retry_apply`、动作注册表、`approvals.apply` 与 `approvals.sweep`（7 天过期、24 小时催办）作业、通知钩子。
- 审批权限独立于模块权限（PER-001）：审批人无需 `approvals:view` 即可审批；可见范围按申请人 / 审批人 / 管理员 / 审计员限定。
- 接口与界面：审批策略（设置 → 审批策略）、审批中心（指标卡、筛选、审阅抽屉、通过/驳回/撤回/评论/重试）、侧栏待办角标、角色编辑器“审批负责人”。
- 未完成：真实来源操作（T-024 起）；站内通知（T-023）；Webhook（T-087）。验证：ruff、`test-postgres.sh`（203 项）、前端检查通过；未目视检查页面。

### 站内通知中心（T-023，已验收）

- 表 `in_app_notifications`（迁移 `0023_notifications`）；服务 `notifications.notify`（同事务写入、站内链接白名单）；接口 `GET /notifications`、`/notifications/unread-count`、`POST /notifications/{id}/read`、`/notifications/read-all`。
- 顶栏铃铛（未读红点、全部/未读面板、点击已读并跳转、60 秒轮询）与 `/notifications` 列表页。已接入：审批（提交/催办/通过/驳回/过期/生效失败）、成员接受邀请、租户状态变更。
- 未完成：评测、预算告警等其他事件由各自任务接入；SSE 推送；保留期清理。验证：ruff、`test-postgres.sh`（210 项）、前端检查通过；未目视检查页面。

### 助理发布接入审批（T-024，已验收）

- 发布与回滚走 `submit_change(event="publish")`：策略关闭立即生效（201），开启返回 202 并建审批单；通过后由 worker 执行动作 `assistant.publish`，上线提交时冻结的快照，成功后通知负责人与申请人。执行前重新校验，失败可在审批中心重试。
- 助理列表/详情显示“审批中”，有待审批单不可删除；预览接口返回审批要求；版本快照关联审批记录。
- 未完成：发布前自动运行测试用例（T-027）；开发租户需第二位管理员或关闭发布审批才能发布。验证：ruff、`test-postgres.sh`（220 项）、前端检查通过；未目视检查页面；`check_runtime_e2e.py` 本机因 URL 校验无法运行。

### 测试控制台与测试用例（T-027，已验收）

- 表 `assistant_test_cases`、`assistant_test_runs`（迁移 `0024_testcases`）；批量运行由 Worker 作业 `testcases.run` 执行，逐条走完整运行管线后由可替换的评测模型判定；发布/回滚提交审批时对待发布快照自动跑一遍并写入审批单检查项。
- 前端：详情页概览右侧实时测试控制台（流式、选版本、添加为用例）、“测试用例”页签（CRUD、运行单条/全部、进度、结果与判定理由）。
- 限制：**仅用假模型验证，评测判定未用真实模型跑过**；检索为桩；环境固定生产；未目视检查页面。验证：ruff、`test-postgres.sh`（248 项）、前端检查通过。

### 登录安全、顶栏环境、生命周期自动流转与网关同步入队（T-004 / T-006 / T-005，已验收）

- T-004：账号连续 5 次失败锁定 15 分钟（423 `auth.locked`）、IP 1 分钟 20 次失败 429、不泄露账号是否存在；会话有效期与空闲超时可配置（默认 12 小时 / 120 分钟），后台轮询不续期；锁定通知企业管理员。迁移 `0025`。
- T-006：`X-Environment` 与 `current_environment` 依赖（非企业版选预发/开发 403）、顶栏环境切换与健康点、侧栏租户卡显示套餐名、`/auth/me` 增加 `plan` 与 `environments`。
- T-005：每小时自动流转（试用到期→停用、宽限 7 天→只读、待删除 30 天→已删除，标注“数据尚未清除”）；平台可创建试用租户；网关同步改为 Worker 作业并立即返回 `pending`；只读/停用租户的虚拟 Key 在 LiteLLM 中被拦截、恢复后解除。迁移 `0026`。
- 验证：ruff、`test-postgres.sh`（291 项）、前端检查；**网关与运行时 e2e 已在真实栈上通过**（含停用租户 Key 被拒绝）。未目视检查页面。

### 知识库文档（T-031，已验收，有条件）

- 上传（扩展名 + 内容嗅探 + 50 MB + 存储配额，同名覆盖/保留两者）、Worker 流水线（上传到引擎 → 退避轮询 → 回写分片数/失败原因，5 分钟清扫兜底）、网页 URL（公网 https 校验、逐跳重定向、20 MB）、重新索引、软删除、下载（attachment + nosniff + CSP sandbox）、分片预览；复制/删除知识库联动文档；RAGFlow 与 Dify 共用同一引擎接口。迁移 `0031`；原文件存在 API 与 Worker 共享的卷 `knowledge_uploads`。
- 验证：ruff、`test-postgres.sh`（445 项）、前端构建与 i18n 检查、假服务端到端（RAGFlow 与 Dify 两种配置）；**Dify 真实账号**验证了 Markdown 的上传→索引→分片→删除。**RAGFlow 未验证；PDF/DOCX/HTML 与大文件耗时未验证；页面未在浏览器里看过。**

### 知识库（T-030，已验收，有条件）

- 知识库列表/创建/详情与设置/复制/删除；Worker 把每个知识库同步为 RAGFlow 的文档与 FAQ 两个数据集（FAQ 库只有 FAQ 数据集）；改向量模型或切片策略触发“新建下一代数据集 → 重新索引 → 切换 → 删旧”的重建，期间旧索引继续服务；RAGFlow 未配置时创建照常成功并提示；每小时补同步。文档数、FAQ 数、引用数通过注册点接入（T-031 / T-032 / T-033）。迁移 `0029`。
- 验证：ruff、`test-postgres.sh`（385 项）、前端构建与 i18n 检查；`check_knowledge_e2e.py` 在真实 API + Worker + **假 RAGFlow**（`scripts/fake_ragflow.py`，compose profile `fake-ragflow`）上通过。**没有真实 RAGFlow：第 0 步对接验证与真实跑通未做；页面未在浏览器里看过。**

### 套餐管理与用量汇总（T-010 / T-011，已验收）

- T-010：平台“套餐与权益”页（发布新版本、应用订阅变更到租户，已有租户保留旧快照；有租户在用的套餐不可删除）；环境可用性改读订阅权益；同一套餐重复应用沿用租户席位。
- T-011：Worker 每 5 分钟从 LiteLLM `/spend/logs/v2` 拉取逐条花费，按 Key 别名归属租户/环境、按 `spend_logs_metadata` 归属助理/来源，按目录扣点换算入 `usage_records`（请求 ID 去重，PostgreSQL advisory lock）；月度点数与会话数配额生效；预算告警每阈值每月一次，100% 可暂停开发/预发（封禁其虚拟 Key，月初或调高预算后恢复）；设置 → 订阅与用量页与 CSV 导出。迁移 `0028`。
- 验证：ruff、`test-postgres.sh`（362 项）、前端构建与 i18n 检查；`check_usage_e2e.py` 在真实栈通过。**只用 `mock_response`，无真实供应商 Key；页面未在浏览器里看过。** 遗留见两个任务文档。

### 租户 API 密钥、开放对话 API 与网站渠道（T-028 / T-040，已验收）

- T-028：设置 → API 密钥（明文只显示一次、只存哈希与掩码、吊销立即生效、成员暂停/移除时吊销其密钥）；开放 API `/v1`（`POST /v1/assistants/{id}/chat`、`GET /v1/conversations/{id}/messages`，生产跑线上版本、开发/预发跑草稿，SSE，统一错误信封，独立 OpenAPI 文档 `/v1/docs`）；按租户 QPS 的 Valkey 令牌桶（429 + Retry-After）。迁移 `0027`。
- T-040：网站渠道（草稿 → 发布快照，受“接入新的外部渠道”审批策略约束；域名白名单 + site_key + 访客令牌；`/v1/widget/*` 与 `widget.js`）；渠道页、助理详情“渠道”页签（含开放 API 接入说明）。
- 验证：ruff、`test-postgres.sh`（337 项）、前端检查；真实栈上 `check_open_api_e2e.py` 全部通过，真实 `widget.js` 经 jsdom 完成一次对话。**只用 LiteLLM `mock_response` 验证，没有真实供应商 Key；未在真实浏览器里验证挂件。**

### 知识挂载与运行时检索（T-033，已验收（有条件）：Dify 已用真实环境验证，RAGFlow 按决定不验证，真实模型未验证）

- 助理可挂载知识库（`mounts.kb`）：`GET /capabilities/candidates?kind=kb` 只列出“已索引”的知识库（按组织范围过滤），创建/编辑校验（只能新挂载已索引的本租户知识库，最多 10 个；已有的失效挂载不阻止保存）；`MOUNT_RESOLVERS["kb"]` 报告 `available` / `disabled`（无已索引内容）/ `deleted`，失效挂载进入配置检查（GLB-083）；知识库“被引用数”统计草稿与线上版本（`knowledge_mounts.py`）。向导第 3 步与详情页“挂载能力”弹窗已接入选择器。
- 运行时：`StubRetriever` 已被 `EngineRetriever` 取代（引擎的 `search` 对 RAGFlow、Dify 都已实现），FAQ 优先、阈值取知识库设置，检索进入 Langfuse span `oc.retrieval`；`retrieval.mode` 取值 `none` / `ragflow` / `dify` / `stub`。引用写入 `done` 事件、`runs.output` 与新列 `messages.citations`（迁移 0032），会话详情、开放 API 响应与测试控制台都能看到；测试控制台的“检索：桩实现”标识已移除。
- 租户安全：运行时只按 `kb_id` 解析租户当前的知识库行得到 dataset，DSL 里写的 dataset ID 不被信任；快照仍记录发布时的引擎与 dataset ID。
- 验证：ruff、迁移升降级 + `alembic check`、全量 472 项后端测试、前端检查通过；真实走 HTTP 的测试（假 RAGFlow / 假 Dify 引擎）覆盖“上传 → 索引 → 挂载 → 提问 → 带引用回答”、未索引/他租户知识库不可挂载、失效挂载、引用计数、引擎失败、删除后不再检索；隔离栈上 `scripts/check_retrieval_e2e.py` 在 RAGFlow、Dify 两种假引擎下均通过（真实 Worker + LiteLLM `mock_response` + Langfuse）。
- **真实 Dify Cloud 验证（2026-11-09）**：`check_retrieval_e2e.py`（`E2E_ENGINE=dify`）在真实 Dify 上通过——上传 Markdown、Dify 真实索引（高质量嵌入）、挂载、提问、语义检索得到带引用的上下文（直接调用 `search` 得分 0.814，阈值 0.62 通过）、删除知识库后不再检索；回答仍由 LiteLLM `mock_response` 模型产生。验证中发现并修复两个只有真实引擎才暴露的问题：Dify 对**没有任何已索引内容的 dataset** 检索会返回 `400 … 404 Not Found`（标准知识库总有一个空的 FAQ dataset），现在只检索有已索引文档 / 已发布 FAQ 的 dataset；以及 Dify 套餐有 API 速率限制（403 “request rate limit of your subscription”），触发时检索按 `runtime.knowledge_error` 失败。
- **仍未验证**：RAGFlow（没有可用的实例）、真实模型下的回答质量、阈值是否合适（默认 0.62 在这份短文档上够用，但只试了一份）、更大的文档与多知识库挂载下的延迟，以及 Dify 速率限制下的表现（每次提问 1 次检索，运行时没有重试）。

### FAQ 生命周期与推送（T-032，已验收，有条件）

- FAQ 表（`faqs`，迁移 0033）与命中记录（`faq_hits`）；状态机 草稿 → 已发布 → 已过期，可取消发布、改日期后重新发布；发布才推送到该库的 FAQ dataset，取消发布 / 过期 / 删除 / 知识库删除时从引擎移除，知识库复制与重建会带上已发布的 FAQ。Dify 上一条 FAQ 是一个单分段文档（分段 API 为付费功能）。
- 每小时任务 `knowledge.expire_faqs`：到期置为已过期（幂等）、卡住的推送标记失败、清理旧命中；运行时检索到的 FAQ 写入命中，列表显示近 30 天命中数。
- 接口 `/knowledge-bases/{id}/faqs`（列表/搜索/筛选、增改删、复制、发布/取消发布、重试、CSV 导入预览与逐行错误）；运营可编辑草稿与导入，发布与删除需要 `faq:publish` / `faq:delete`。前端知识库详情页 FAQ 页签已实现。
- 验证：ruff、迁移升降级 + `alembic check`、全量 507 项、前端检查通过；假 Dify / 假 RAGFlow 覆盖“草稿检索不到、发布后以 FAQ 引用作答”。**未做真实 Dify 端到端、未在浏览器目视验收；审批未接入；FAQ 检索阈值待调。**

### 缺陷修复：退出登录与租户编辑（2026-11-09）

- **退出登录 403**：写请求的来源校验要求 `Origin` 与 `APP_ORIGIN`（`http://localhost:5173`）完全相同，用 `http://127.0.0.1:5173` 打开页面时登录能成功（登录不校验），之后所有写操作（含退出登录、租户生命周期）都被 403 “请求来源不合法”。现在开发环境下 `localhost` / `127.0.0.1` / `[::1]` 同端口互通（`Settings.origin_allowed`），非开发环境仍要求完全一致。真实栈上两种来源登录后退出均返回 204。
- **租户编辑**：`PATCH /platform/tenants/{id}` 可改创建时的全部字段——名称、工作区标识（唯一）、套餐、席位上限（不低于已使用）、数据区域、首位企业管理员的姓名/邮箱（唯一）/重置密码（其现有会话失效）、试用中租户的到期日；已删除的租户 409；套餐/席位变更写新的订阅快照，全部审计为 `tenant.updated`（密码只记“已重置”）。租户列表的“编辑”弹窗与创建表单共用同一组字段组件（`TenantFields`）。
- **生命周期不再双人复核**：停用、进入待删除、永久删除现在直接生效（原因可选，仍审计）。由配置 `TENANT_LIFECYCLE_REVIEW`（默认 `false`）控制，设为 `true` 即恢复 PRD PLT-052 的复核流程；已有的待复核请求仍可复核。这偏离 PRD 3.12.6 / PLT-052，是按产品负责人的决定暂时关闭。
- 验证：ruff、全量 512 项通过；前端检查通过；未做浏览器目视验收。
- **知识库页显示“前端框架已就绪”占位页**（T-030 起的前端路由缺陷，不是权限或 Dify 问题）：侧栏每一项都会自动生成占位页路由，`/knowledge` 没有被排除，占位页路由排在真实页面前面抢先匹配，知识库列表/创建/详情页（含 FAQ 页签）因此一直没法从界面进入。现在用 `REAL_PAGES` 集合排除所有已有真实页面的入口；`/capabilities`、`/workflows` 仍是占位页（对应任务未做）。**浏览器里没有目视验证，请刷新页面后确认。**

### 统一前端样式（2026-11-09）

- 新增设计规范 [`design-system.md`](./design-system.md) 与统一样式层 `frontend/src/design-system.css`（在 `App.css` 之后加载）；字号、字重、圆角、控件高度等令牌在 `index.css`。
- 全部页面共用：页面标题 24px、小标题 11px、副标题 13px；卡片标题 16px；按钮/输入/下拉 13px、高 36px；表格表头 12px、单元格 13px；徽章 11px。`App.css` 里原来 260 处零散的 8–27px 字号全部换成 7 档令牌，新增 `npm run style:check` 防止再写具体 px 字号。
- 之前不一致的原因：没有显式字号的页面（助理、知识库等）落回浏览器默认（h1 约 32px、正文 16px、输入框默认字体），其余页面又写死 9–13px。已统一页头（含各页历史类名）、返回链接（`.back-link`）、搜索框、裸表格（`.data-table`）和平台侧栏分组间距。
- 验证：`tsc -b`、lint、`i18n:check`、`style:check`、build 通过；用无头 Chrome 截图逐页对比了企业端（工作台、助理列表/详情/编排页签、OC、版本、知识库列表/详情/创建、审批、审计、成员、设置各页、通知、渠道）与平台端（总览、租户、后台任务、模型、套餐、审计）。
- 注意：Docker 里的前端开发服务器对挂载目录的文件变更有时不会热更新（改了 TSX 页面仍显示旧版），遇到页面与代码不一致时先 `docker compose restart web`。

### 平台 PII 规则与安全基线（T-050，已验收（有条件））

- 平台后台新增“PII 规则”“安全基线”页：5 条默认平台 PII 规则（OC-013）与 7 条预置基线（PLT-140）随迁移 0034 写入；规则的新增、编辑、启停、删除、搜索与状态筛选、正则/保留天数校验已实现；基线里的 PII 条目是规则集引用，不重复存匹配内容。
- 强制规则的新增、停用、下线、删除走双人复核（72 小时失效，发起人不能复核，驳回必填原因；`PLATFORM_RULE_REVIEW` 可关闭）；其余变更直接生效。全部写操作写平台审计。
- 服务接口：`baseline_rules_for_tenant`（按套餐 / 区域 / 行业下发，草稿不下发）、`retention_limit_for_tenant`（租户保留期上限与默认值，已接入“数据与隐私”页）、`list_platform_pii_rules`、`resolve_ruleset`，供 T-052 / T-055 与运行时使用。
- 验证：ruff、迁移升降级 + `alembic check`、全量 547 项、前端检查通过，无头 Chrome 看过页面与弹窗。**规则尚未在运行时执行（T-055）；租户行业字段待 T-051；未做浏览器里的完整复核点击流程。**

### 统一行业字典与平台行业术语表（T-051，已验收（有条件））

- 平台统一行业字典 `industries`（6 个初始行业，可新增、改名、停用；被引用的行业不能删除），租户新增 `industry` 字段（创建 / 编辑租户时选择，默认通用办公）；T-050 的“金融行业”下发范围现在按租户行业（banking / insurance）判断。
- 平台后台“行业术语表”页：术语库列表与术语表、搜索（术语与别名）、新增 / 编辑 / 删除术语与术语库（有租户继承时二次确认）、CSV / TXT 导入（预览识别条数与错误行、重复术语跳过或覆盖）；解析器 `glossary_parser.parse_terms` 是纯函数，供 T-053 复用；预置零售电商、保险、银行金融三库。
- 验证：ruff、迁移升降级 + `alembic check`、全量 568 项、前端检查通过，无头 Chrome 看过页面。**继承数暂按同行业租户计（T-053 改）；术语尚未在运行时使用；未在浏览器里走完导入流程。**

### 继承覆盖引擎与租户规则库（T-052，已验收（有条件））

- 纯函数引擎 `governance/inheritance.py` 按 OC-012 计算平台 → 租户 → 助手三层的生效 PII 规则（覆盖、强制规则、三个开关、计数），T-053 / T-055 复用；租户层存储 `tenant_pii_rules`（自建或覆盖，`from_id` 指向被覆盖的平台规则）与继承开关 `tenant_pii_settings`（迁移 0036）。
- “设置 → 规则库”：继承链、继承平台规则开关、全部生效 / 仅租户级筛选、编辑继承规则生成覆盖、切换启用生成覆盖、恢复、删除自建；变更受“助理规则变更”审批策略约束；平台 PII 规则页与租户页共用 `components/pii/` 的表格与弹窗（三种模式）。
- 验证：ruff、迁移升降级 + `alembic check`、全量 599 项、前端检查通过，无头 Chrome 看过页面。**助手层与运行时执行在 T-055；未在浏览器里点过编辑 / 恢复 / 审批流程。**

### 租户术语表（T-053，已验收（有条件））

- 纯函数引擎 `governance/term_inheritance.py`：行业术语库（租户多选并排序，同名术语取靠前的）→ 租户（覆盖、隐藏、自建）→ 助手（含“继承租户术语表”开关）；租户层存储 `tenant_terms` 与继承选择 `tenant_glossary_settings`（迁移 0037），术语读时计算、不复制。
- “设置 → 术语表”：继承链、行业标签（切换与调整顺序）、术语表（层级、编辑生成覆盖、恢复、删除即隐藏、“本层已删除 N 条继承术语 · 全部恢复”）、导入（复用平台解析器与导入弹窗，预览与重复处理）；变更受“助理规则变更”审批策略约束；新租户开通时按行业预置继承；平台术语库的“继承租户数”改为实际继承数。
- 验证：ruff、迁移升降级 + `alembic check`、全量 636 项、前端检查通过，无头 Chrome 看过页面。**术语尚未在运行时使用（T-057）；助手层在 T-055；未在浏览器里点过完整流程。**

### OC · PII 与安全基线执行（T-055，已验收（有条件））

- 运行时 `pii_input` / `pii_output` 阶段执行平台 → 租户 → 助手三层 PII 规则与下发给租户的安全基线（提示词攻击、内容安全）：拦截时不检索、不调模型直接返回拦截提示，脱敏后的消息才进入记忆 / 检索 / 模型，输出规则存在时回复先过规则再一次性下发；命中只记规则 / 层级 / 动作 / 阶段，不记原文。检测器：正则（带 50 ms 超时）、关键词、Presidio（`classifier`，可选 compose profile `pii`）、LiteLLM 判定模型（`llm_judge`）；外部检测器不可用时放行并记“检测器失败”。
- 会话日志、运行内容与 trace 的脱敏改用该租户生效的规则（无脱敏规则时回退固定模式）；OC 页“PII 规则”阶段（助手层、继承链三级、两个开关、覆盖 / 恢复 / 自建）与编排总览 PII 摘要已可用。
- 验证：ruff、全量 677 项（含 T-053）、前端检查通过；100 条规则 PII 阶段 p95 约 0.5 ms；无头 Chrome 看过页面。**未连真实 Presidio / 判定模型；未在浏览器里点过助手层规则流程；工具返回护栏在 T-071。**

## 4. 下一步

**优先线（2026-10-28 起）**：先做“闭环切片”——T-010a → T-020 → T-025 → T-026 → T-054a → T-056a，见 [`tasks/README.md`](./tasks/README.md)“闭环切片”。知识检索用 `Retriever` 协议 + 桩实现，**未接真实 RAGFlow**，桩存在期间不得把知识问答记为完成。

开发任务已全部拆分到 [`tasks/README.md`](./tasks/README.md)：试点版 V0.1 共 22 个任务，V0.2 / V0.3 / V1.0 共 70 个任务（含依赖、外部条件、推荐并行批次、开发负责人的可逆决定与待产品确认事项）；其中 T-106（平台角色分权）等待产品决策；多租户已决定 V1.0 只做共享隔离，T-127（增强/独享隔离与多区域）延后到 V1.0 之后。需要用户提供的外部条件：

1. 首发模型供应商 API Key（至少 1 个对话模型；T-025/T-027/T-028 的真实调用验收需要）。
2. （已提供）Dify Cloud 知识库 API 密钥，已写入 `.env`；RAGFlow 不再作为依赖。

## 5. 交接记录

```text
更新时间：2026-10-22
完成的 PRD 条款/功能：PRD 矛盾修正（环境命名、GLB-068、GLB-007）；PER-004；PLT-050–053 首版；RUL-004；6.1 审计员模型权限；LiteLLM 网关同步与连通测试；RAGFlow/Langfuse 客户端与状态检查；开发计划修订（引擎接入方式、MVP 切线、遗漏功能归属、前移依赖、阻塞决策）
改动文件或迁移：backend/src/beework/api/*（由 routes.py 拆分）、tenant_access.py、tenant_lifecycle.py、gateway_sync.py、model_testing.py、integrations/*；迁移 0011_lifecycle_and_staging、0012_gateway_sync；frontend/src/pages/*（由 App.tsx 拆分）；compose.yaml（litellm、litellm-db-init）；config/litellm/config.yaml；scripts/test-postgres.sh；.github/workflows/ci.yml
数据/API/界面状态：见第 3 节
授权与审计覆盖：生命周期发起/执行/驳回、模型测试均写审计；平台对租户的操作双写
验证命令和结果：见第 3 节「本轮验证记录」
仍未完成/已知限制：见第 3 节各「未完成」与第 4 节
下一项及其依赖：第 4 节
```

## 6. 持续适用的约束

- 模块化单体控制面；Worker 独立进程；第三方引擎（LiteLLM、RAGFlow、Langfuse）只经 HTTP API 接入，不读写其内部存储，业务事实以 BeeWork PostgreSQL 为准。
- 新租户资源必须服务端强制 tenant scope；RAGFlow dataset、LiteLLM team/key 按租户（及环境）划分，检索只传入有权使用的 dataset。
- 已发布执行配置使用不可变版本/快照；并发修改需冲突检测。
- 所有权限在后端逐资源、逐操作检查；前端隐藏菜单只是体验层。
- 模型、Embedding 请求统一走 LiteLLM；浏览器不持有供应商或网关密钥。
- 新迁移的 revision ID 不超过 32 个字符（`alembic_version.version_num` 是 `varchar(32)`）。
- 加租户行锁使用 `api.common.lock_tenant`（`FOR NO KEY UPDATE`），不要再用裸 `with_for_update()` 锁 `tenants`。
- 每次修改前先看 `git status --short`；未经用户要求不提交。
