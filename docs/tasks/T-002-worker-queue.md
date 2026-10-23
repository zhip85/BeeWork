# T-002 Worker 与任务队列、运行事件推送

**阶段**：0（阶段 3 硬前置）　**依赖**：T-001　**PRD**：NFR-020、5.5 第 11 步

## 目标

建立后台任务执行能力：业务写入与入队同事务、至少一次投递、幂等处理、可重试、可观测；并提供运行事件的实时推送通道，供 LangGraph 运行（T-025）使用。

## 设计（已定，按此实现）

- **队列存储**：PostgreSQL 表 `jobs`：`id`、`kind`、`payload`(JSON)、`tenant_id`(可空)、`status`(queued/running/succeeded/failed/dead)、`attempts`、`max_attempts`(默认 5)、`run_after`、`locked_by`、`locked_at`、`last_error`、`idempotency_key`(唯一，可空)、`created_at`、`finished_at`。
- **入队**：`enqueue(db, kind, payload, *, tenant_id=None, idempotency_key=None, run_after=None)`，只 `db.add`，由调用方的事务提交（outbox 语义）；相同 `idempotency_key` 不重复入队。
- **取任务**：`SELECT ... WHERE status='queued' AND run_after<=now() ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT n`；运行超时（`locked_at` 超过 N 分钟）的任务由回收逻辑放回队列。
- **处理器注册**：`@job_handler("kind")` 装饰器；处理器签名 `(db, payload) -> None`；抛异常则指数退避（如 30s × 2^attempts，上限 1h），超过 `max_attempts` 置 `dead`。
- **定时任务**：Worker 内置简单调度表（如每 5 分钟、每天），到点入队，用 `idempotency_key=kind:时间窗` 防重复。
- **进程**：`python -m beework.worker`；Compose 新增 `worker` 服务（与 api 同镜像），并把现有 `audit-retention` 改为 Worker 的每日定时任务后删除该服务。
- **运行事件**：表 `run_events`（`run_id`、`seq`、`type`、`data` JSON、`created_at`），`publish_run_event(db, run_id, type, data)` 写表后提交，再 `PUBLISH run:{run_id}` 到 Valkey；`stream_run_events(run_id, after_seq)` 生成器先回放表中事件再订阅频道，供 SSE 使用。
- **平台查看**：`GET /api/v1/platform/jobs?status=&kind=`（平台管理员，分页）与 `POST /platform/jobs/{id}/retry`，平台后台加一个简单的任务列表页（可放在“运维”菜单下）。

## 验收标准

- [x] 迁移新增 `jobs`、`run_events`。
- [x] PostgreSQL 测试：多个 Worker 并发取任务不重复执行；失败重试与 `dead`；`idempotency_key` 去重；锁超时回收。
- [x] 审计保留清理迁移为 Worker 定时任务，`compose.yaml` 去掉 `audit-retention`，README 同步。
- [x] 一个示例处理器 `noop.echo` 与端到端测试（入队 → Worker 执行 → 状态 succeeded）。
- [x] `run_events` 的回放 + 订阅有测试（Valkey 可在测试中用 fakeredis 或真实 Compose Valkey，二选一并说明）。

## 不在范围

- 引入 Celery/RQ/Kafka 等额外中间件（不需要）。

## 完成记录

**负责人**：GPT-5.6（Codex）完成首版实现；Claude Sonnet 5.5 审查、修复缺陷并补全测试与文档。

**改动文件**
- 新增：`backend/src/beework/worker/{queue,events,scheduler,runner,__main__}.py`、`models/jobs.py`、`schemas/jobs.py`、`api/jobs.py`、迁移 `0013_worker_queue`、`tests/test_worker.py`、前端 `pages/platform/PlatformJobsPage.tsx`。
- 修改：`models/__init__.py`、`api/__init__.py`（各注册一行）、`compose.yaml`（`worker` 取代 `audit-retention`）、README、`architecture.md` 3.6、前端平台控制台入口与类型。

**迁移**：`0013_worker_queue`（`jobs`、`run_events`，含 `run_events.tenant_id`），可降级，`alembic check` 无差异。

**接口**：`GET /api/v1/platform/jobs`（状态/类型筛选、分页）、`POST /api/v1/platform/jobs/{id}/retry`（写审计 `job.retried`）、`GET /api/v1/runs/{run_id}/events`（SSE，租户隔离）。

**验证（2026-10-23）**
- `uvx ruff@0.14.0 check .`：通过。
- 迁移 upgrade → `alembic check` → downgrade base → upgrade，并跑全量 62 项测试（含 PostgreSQL 专用测试，无跳过）：通过；为避免影响开发库，在一次性容器和临时库上执行。
- 真实启动 `python -m beework.worker`：入队的 `noop.echo` 被取走并成功，每日 `audit.retention` 自动入队并成功。
- 前端 `tsc -b`、`npm run lint`、`npm run build`：通过。
- **Valkey**：事件订阅测试使用真实 Compose Valkey（`test_stream_receives_live_events_through_real_valkey`，不可达时自动跳过），另有用假订阅对象的单元测试覆盖时序边界。**未连接任何真实第三方引擎。**

**审查中发现并修复的缺陷（首版）**
1. 两个 Worker 并发入队同一个 `idempotency_key` 时，后提交者抛 `IntegrityError`，且主循环无异常处理，整个 Worker 进程退出 → 入队改用保存点并在冲突时返回已有任务；主循环捕获异常、退避后继续（PostgreSQL 并发测试覆盖）。
2. 事件流先回放历史再订阅，两者之间发布的事件会丢失 → 改为先订阅、再以数据库为唯一来源读取，Valkey 仅作唤醒，并有轮询兜底（Valkey 不可用时仍可用）。
3. `GET /runs/{id}/events` 只要求登录，任何人可订阅任意 run → 事件表加 `tenant_id`，接口要求租户成员身份，非本租户或不存在一律 404。
4. 事件流永不结束且不释放连接 → 终止事件（`done`/`error`/`cancelled`）结束流；空闲时发送 SSE 心跳以发现断开；流使用独立数据库会话。
5. 其他：清理失败重试里的死代码；补定时任务幂等、指数退避、未知任务类型、Valkey 故障、并发 `seq` 等测试。

**遗留问题**
- `GET /runs/{id}/events` 目前依据 `run_events.tenant_id` 校验；T-025 建立 `runs` 表后应改为按 run 记录校验租户、助理范围与权限，并要求创建 run 时同事务发布 `queued` 事件。
- 浏览器 `EventSource` 无法自定义 `X-Tenant-ID` 头，前端需要用 `fetch` 流式读取（T-025/T-027 实现时处理）。
- 锁超时固定 15 分钟：运行超过 15 分钟的处理器会被重复执行；长任务需要拆分或心跳续锁（暂无此类任务）。
- 平台任务页只做了列表、筛选与重试，没有任务详情抽屉与自动刷新。
