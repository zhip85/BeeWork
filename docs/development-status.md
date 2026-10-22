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
| 0 | 工程底座 | 大部分完成 | Compose、迁移、按领域拆分的前后端代码、ruff/oxlint、SQLite + PostgreSQL 测试、CI 已就绪；**Worker/队列未引入（阶段 3 前置）**。 |
| 1 | 租户、组织、角色权限与审计 | 收尾中 | 缺：i18n 框架、登录失败锁定、邀请邮件、生产清理调度/告警、外部存证。 |
| 2 | 平台定义、套餐权益与模型配置 | 进行中 | 已完成：模型目录、环境配置（非企业版仅生产）、租户生命周期 7 态 + 双人复核、LiteLLM 同步与连通测试。缺：套餐版本/权益/配额、BYOK、跨环境发布、真实供应商调用验收（缺供应商 Key）。 |
| 3 | 助理构建、审批、版本与最小运行闭环 | 未开始 | 前置：Worker/队列、首发模型供应商。 |
| 4 | 知识库（RAGFlow） | 未开始 | 已有 RAGFlow API 客户端（仅契约测试，未对接真实实例）；需先部署 RAGFlow。 |
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
| 知识（3.4） | 未开始 | RAGFlow 客户端已就绪，业务模块未建。 |
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
- 平台总览显示 LiteLLM / RAGFlow / Langfuse 的连接状态。
- **未完成**：真实供应商调用验收（缺 Key）；LiteLLM 花费数据汇总为点数；BYOK；跨环境发布与审批；套餐权益与配额。

### RAGFlow 与 Langfuse

- `backend/src/beework/integrations/ragflow.py`：数据集、文档上传、解析、状态、删除、检索的 API 客户端；`langfuse.py`：连通性检查。
- RAGFlow：**只有契约测试，尚未对接真实实例**。已决定使用 RAGFlow Cloud（需付费版才有 API Key）；Cloud API 与自托管版的兼容性在 T-030 第 0 步实测。
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

### API 公共约定（T-001，待验收）

- `api/conventions.py`：`ApiError` 统一错误格式、`Pagination`/`Page[T]`/`paginate`、`VersionedMixin` + `check_row_version`（409）、通用列 mixin、`require_platform_admin`、名称校验、`validate_public_https_url`（SSRF）、`ensure_resource_in_scope`/`scope_filter`。平台接口已全部改用它；旧 `HTTPException` 兼容（补 `code: "error"`）。
- 验证：ruff、`test-postgres.sh`（42 项）、前端检查、网关 e2e 均通过。已知限制：422 校验错误仍无 `code`。

## 4. 下一步

开发任务已拆分到 [`tasks/README.md`](./tasks/README.md)（试点版 V0.1 共 22 个任务，含依赖、外部条件与推荐并行批次）。需要用户提供的外部条件：

1. 首发模型供应商 API Key（至少 1 个对话模型；T-025/T-027/T-028 的真实调用验收需要）。
2. RAGFlow Cloud 付费版 API Key（T-030 起）。

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
