# BeeWork

BeeWork 企业级 AI SaaS 平台。当前处于基础建设阶段：Docker Compose 提供 React/TypeScript、FastAPI、PostgreSQL、Valkey 和 LiteLLM（AI 网关）本地环境；RAGFlow（知识引擎）与 Langfuse（调用追踪）按地址和密钥接入独立部署的实例；已实现租户/组织/角色管理、审计首版和平台模型目录首版，但尚未形成助理构建、审批、发布、知识检索和真实模型运行的业务闭环。最小 LangGraph 示例仅用于验证依赖，不是产品运行时。

参与开发先读 [`AGENTS.md`](./AGENTS.md)。文档分工：[工程规范](./docs/architecture.md)、[任务看板](./docs/tasks/README.md)、[路线图与决策](./docs/technical-plan.md)、[开发进度](./docs/development-status.md)、[PRD](./docs/BeeWork-PRD.md)。

## 本地启动

需要 Docker Desktop 和 Docker Compose v2。

```bash
cp .env.example .env
```

`.env.example` 使用仅供本机开发的默认密码；如需修改，确保 `DATABASE_URL` 中的密码与 `POSTGRES_PASSWORD` 一致。`.env` 仅用于本地开发，不要提交或用于生产环境。随后启动：

```bash
docker compose up --build -d
docker compose ps
```

若 Docker 构建时无法连接默认 Python 包索引，可在 `.env` 中设置可用的 `PIP_INDEX_URL` 镜像地址后重试构建。

前端工作台：<http://localhost:5173>  
后端 API 文档：<http://localhost:8000/docs>

首次启动时，API 会运行 Alembic 数据库迁移，并在开发环境创建一组分离的本地账号：

```text
SaaS 平台管理员：platform@beework.dev / BeeWork-Platform-2026!
平台复核管理员：  reviewer@beework.dev / BeeWork-Reviewer-2026!（用于双人复核）
企业管理员：      admin@beework.dev / Beework-Dev-2026!
```

登录页可分别选择“企业工作区”或“SaaS 平台管理”。平台管理员没有企业成员关系，不能直接进入企业工作区；企业管理员只管理所属企业，不能查看或操作平台租户。平台创建企业时会建立独立的首位企业管理员账号，不会把平台管理员加入新企业。创建租户需要填写企业管理员姓名、邮箱和初始密码，并通过安全渠道交付凭据。

以上默认凭据仅用于本地开发。可通过 `BOOTSTRAP_ADMIN_*` 和 `BOOTSTRAP_PLATFORM_ADMIN_*` 环境变量分别配置；两组邮箱必须不同。生产环境必须关闭开发引导（`ENVIRONMENT` 设为 `production`）、设置至少 32 位随机 `AUTH_SECRET` 和独立的 `MODEL_CREDENTIALS_SECRET`、配置 `APP_ORIGIN` 为实际前端源，并启用 `AUTH_COOKIE_SECURE=true`；还应通过安全渠道建立生产平台管理员和企业管理员账号。模型凭证密钥用于解密数据库中的上游模型 API Key，必须存入密钥管理器并纳入备份/轮换流程；密钥丢失会导致已保存的凭证无法解密。

验证服务就绪并运行 LangGraph 示例：

```bash
curl http://localhost:8000/health/ready
curl -X POST http://localhost:8000/api/v1/runtime/demo \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello"}'
```

预期示例响应：

```json
{"result":"LangGraph is ready: hello"}
```

运行测试：

```bash
# SQLite 单元测试（PostgreSQL 专用测试会跳过）
docker compose exec api python -m unittest discover -s /app/tests
# 完整套件：在一次性数据库 beework_test 上做迁移升降级，并运行 PostgreSQL 并发/触发器测试
./scripts/test-postgres.sh
# 静态检查
(cd backend && uvx ruff@0.14.0 check .)
docker compose exec web sh -c "npx tsc -b && npm run lint"
```

CI（`.github/workflows/ci.yml`）在 push/PR 时执行同样的检查。

身份与租户 API：

- `POST /api/v1/auth/login`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me`
- `GET /api/v1/tenants`：当前用户加入的企业
- `GET /api/v1/members`：列出选定企业成员、邀请及席位使用量（`X-Tenant-ID`）
- `POST /api/v1/members/invitations`：批量邀请邮箱，返回仅本次可见的邀请链接
- `POST /api/v1/members/invitations/accept`：使用邀请令牌和密码接受邀请并建立登录会话
- `POST /api/v1/members/invitations/{invitation_id}/resend`：重新生成 7 天有效的一次性邀请链接
- `DELETE /api/v1/members/invitations/{invitation_id}`：撤销待处理邀请
- `PATCH /api/v1/members/{user_id}/role`：企业管理员调整成员内置角色，或指派一个同租户的启用中自定义角色
- `PATCH /api/v1/members/{user_id}/organization`：将成员调整到同一企业内的部门
- `PATCH /api/v1/members/{user_id}/status`：暂停或恢复企业成员；暂停会撤销该账号现有登录会话
- `DELETE /api/v1/members/{user_id}`：移除企业成员关系
- `GET /api/v1/organizations`：需要 `organizations:view`；部门范围自定义角色只返回授权部门及其下级
- `POST /api/v1/organizations`、`PATCH /api/v1/organizations/{id}`、`DELETE /api/v1/organizations/{id}`：企业管理员创建、重命名或删除部门
- `GET /api/v1/roles/catalog`、`GET /api/v1/roles`：企业管理员查看权限目录和内置/自定义角色
- `POST /api/v1/roles`、`PUT /api/v1/roles/{id}`、`DELETE /api/v1/roles/{id}`：企业管理员创建、更新、停用或删除自定义角色；有成员使用的角色不能停用或删除
- `GET /api/v1/auth/me`：每个企业成员返回角色信息及有效权限集合，供前端导航和路由守卫使用
- `GET /api/v1/audit/events`：按分类、操作人/内容、结果和时间范围筛选租户审计记录，支持分页
- `GET /api/v1/audit/events/export`：按相同筛选条件导出 CSV，需要 `audit:export`
- `GET /api/v1/audit/integrity`：验证当前租户审计哈希链并报告未纳入哈希链的历史记录数
- `GET /api/v1/platform/overview`：平台管理员查看租户和成员总览
- `GET/POST /api/v1/platform/tenants`：平台管理员查看/创建企业；创建时同时设置独立的首位企业管理员
- `POST /api/v1/platform/tenants/{tenant_id}/lifecycle`：按 PRD 3.12.6 执行生命周期操作（转正式、延长试用、标记欠费、登记回款、转只读、停用、恢复、进入待删除、永久删除）；停用/待删除/永久删除须填写原因并返回 202，等待另一名平台管理员复核
- `GET /api/v1/platform/operations`、`POST /api/v1/platform/operations/{id}/approve|reject`：待复核高危操作；复核人不能是发起人，72 小时未处理自动驳回
- `GET /api/v1/platform/integrations`：LiteLLM / RAGFlow / Langfuse 的配置与连通状态
- `GET /api/v1/platform/audit/events`、`GET /api/v1/platform/audit/events/export`：平台管理员独立查询/导出平台操作日志，不包含租户业务审计
- `GET /api/v1/platform/audit/integrity`：平台管理员验证平台审计链；可传 `tenant_id` 验证指定租户链
- `GET /api/v1/platform/models`：平台管理员查询模型目录，支持 `search` 和 `status` 筛选
- `POST /api/v1/platform/models`、`PUT /api/v1/platform/models/{model_id}`：平台管理员新增/编辑模型目录；密钥仅写入、加密存储并以掩码回显；保存后同步为 LiteLLM deployment（别名 `bw-{模型ID}`）
- `POST /api/v1/platform/models/{model_id}/sync`、`POST /api/v1/platform/models/{model_id}/test`：重试网关同步；经网关做连通性测试（`mock: true` 只验证链路，不调用供应商）
- `GET /api/v1/tenant/model-environments/{development|staging|production}`：按当前租户套餐、区域和模型目录开放范围查询对应环境的可用模型与启用/默认配置（需 `models:view`）；非企业版只有 `production`（RUL-004）
- `PUT /api/v1/tenant/model-environments/{environment}`：保存环境启用模型和默认模型；需 `models:edit` 与 CSRF，使用 `expected_version` 检测并发修改；保存后在 LiteLLM 中同步本环境的 team 与虚拟 Key（只放行已启用模型）。响应不含供应商 endpoint 或凭证。
- `POST /api/v1/tenant/model-environments/{environment}/sync`、`.../test`：重试网关同步；用本环境自己的虚拟 Key 测试调用已启用模型
- `POST /api/v1/runtime/demo`：需登录，用于验证 LangGraph 示例

浏览器会话使用 HttpOnly Cookie；状态变更请求还需 `X-CSRF-Token`。组织架构和成员管理的写操作须由企业管理员操作；根组织不可删除/重命名，有成员或子部门的部门不可删除。数据库复合外键保证组织、角色范围和成员关联不能跨租户。组织范围角色指派时会校验成员部门，成员调部门或调整角色范围时也会避免成员越出授权范围。

成员邀请链接有效 7 天，令牌只以摘要形式存储；创建或重新发送时链接只在响应中返回一次。项目尚未接入邮件投递服务，因此管理员需手动通过安全渠道转发链接。租户席位上限目前作为租户字段在平台开通时配置，默认界面值为 10，后续套餐权益模块应接管该配置。暂停/移除会撤销该用户的现有会话；个人 API Key 及已有业务资源负责人转移尚未实现，因为对应资源模块还未建立。

成员生命周期迁移建立了持久化会话校验；升级前签发的旧登录 Cookie 不包含会话记录，升级后需要重新登录一次。

自定义角色权限按模块矩阵配置；内置角色权限不可编辑，可复制为自定义角色模板。前端菜单/路由按 `/auth/me` 权限集合控制。组织树读取 API 使用独立的 `organizations:view` 并校验组织范围；审计读取与 CSV 导出分别要求 `audit:view` / `audit:export`。其他尚未实现的业务模块在其 API 落地时仍需接入服务端权限依赖，不能只依靠前端隐藏菜单。

Compose 中的 `audit-retention` 服务会每 24 小时清理一次超过 365 天的审计记录；需要手动执行时可运行 `docker compose exec api python -m beework.audit_maintenance`。生产部署应将该维护进程纳入生产编排/调度与告警，并遵循备份/保留政策。

平台端目前提供独立侧栏、平台总览、租户生命周期管理、独立平台操作审计及平台模型目录首版。平台开通企业时可指定套餐与区域；租户管理员可按环境配置模型启用和默认值。套餐权益/配额、BYOK、审批驱动的跨环境发布、真实网关调用和计量尚未完成。渠道类型、Widget 模板、行业字典、PII/安全基线和运维工单也尚未接入真实业务数据。

成员自助密码重置、SSO/MFA、助理级数据范围和其余业务域 API 尚未实现。审计新事件使用 SHA-256 哈希链；迁移前历史记录不会回填链哈希，验证结果会明确报告未链记录数。`audit-retention` Compose 服务每 24 小时清理超过 365 天的事件并保留链 checkpoint；单次手动执行可运行 `docker compose exec api python -m beework.audit_maintenance`。数据库触发器阻止常规 UPDATE/DELETE，不等于外部 WORM 或抗数据库高权限攻击；生产仍需独立不可变存储/签名存证和访问分离。新审计事件捕获请求来源 IP；无法恢复的历史 IP 显示为「—」。

本地开发的 `MODEL_CREDENTIALS_SECRET` 是示例值，不可用于生产。它加密平台模型目录中的上游 API Key；生产部署应为其配置独立随机密钥，持久保管且避免在更换密钥后遗失解密能力。

## 第三方引擎接入

BeeWork 自研控制面和 LangGraph 运行时；模型网关、知识引擎和调用追踪使用开源产品的官方镜像，平台只调用它们的 HTTP API：

| 引擎 | 作用 | 部署 | 配置 |
|---|---|---|---|
| LiteLLM 1.106.0 | AI 网关：模型路由、租户虚拟 Key、限流、花费记录 | 本地 Compose 服务 `litellm`（<http://localhost:4000>，镜像按 digest 固定），数据存在同一 PostgreSQL 的 `litellm` 库 | `LITELLM_BASE_URL`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY` |
| RAGFlow | 知识引擎：解析、切片、向量化、检索 | RAGFlow Cloud（付费版才提供 API Key）；本机内存不足以自托管 | `RAGFLOW_BASE_URL`、`RAGFLOW_API_KEY` |
| Langfuse | 调用追踪与评测 | Langfuse Cloud（只用 OpenTelemetry 写入与 `/api/public/v2/*` 读取；旧接口 2026-11-16 下线） | `LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` |

平台总览页显示三者的连接状态。模型目录保存后自动同步到 LiteLLM；“链路检查”用平台或租户的网关凭证请求 LiteLLM `/v1/models`，只校验凭证与模型权限、不调用供应商；“真实调用”需要在模型目录中填写有效的供应商 API Key。整条链路可用 `docker compose exec api python /app/scripts/check_gateway_e2e.py` 验证（会在模型目录留下已停用的 `E2E Mock Chat` 测试条目）。

停止容器但保留数据库卷：

```bash
docker compose down
```

本地数据库和 Valkey 端口只绑定到 `127.0.0.1`。当前数据库密码默认值仅适用于本地开发；生产环境必须使用密钥管理和专用部署配置。

前端开发依赖 React、React Router、React Flow、Formily 和 Lucide；组织与成员管理已接入真实 API，其余尚未实现的模块页面仍使用开发期示例或占位内容。
