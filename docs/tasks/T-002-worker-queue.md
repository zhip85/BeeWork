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

- [ ] 迁移新增 `jobs`、`run_events`。
- [ ] PostgreSQL 测试：多个 Worker 并发取任务不重复执行；失败重试与 `dead`；`idempotency_key` 去重；锁超时回收。
- [ ] 审计保留清理迁移为 Worker 定时任务，`compose.yaml` 去掉 `audit-retention`，README 同步。
- [ ] 一个示例处理器 `noop.echo` 与端到端测试（入队 → Worker 执行 → 状态 succeeded）。
- [ ] `run_events` 的回放 + 订阅有测试（Valkey 可在测试中用 fakeredis 或真实 Compose Valkey，二选一并说明）。

## 不在范围

- 引入 Celery/RQ/Kafka 等额外中间件（不需要）。
