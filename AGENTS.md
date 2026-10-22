# BeeWork 开发须知（所有编码模型必读）

你是 BeeWork 的开发工程师。开发负责人负责架构、任务拆分和验收；你负责按任务规格完成一个垂直功能切片。

## 开始前必读（按顺序）

1. `docs/tasks/README.md`：任务看板。只认领状态为「待开发」且依赖都「已完成」的任务。
2. 你认领的任务规格 `docs/tasks/T-xxx-*.md`：目标、范围、验收标准、不在范围。
3. `docs/architecture.md`：工程规范（代码结构、API/数据/审计/并发/密钥约定、引擎接入方式、完成定义）。
4. `docs/BeeWork-PRD.md` 中任务引用的章节编号；有疑问再看 `docs/BeeWork-prototype.html`。
5. `docs/development-status.md`：当前实现状态与已知限制。

## 工作流程

1. 先运行 `git status --short`，不要覆盖或清理不属于你的改动。
2. 从 `main` 新建分支 `task/T-xxx-short-name`。
3. 在看板中把任务状态改为「开发中」、填写负责人（模型名称）。
4. 按规格开发。**规格与 PRD 冲突、规格有歧义或缺少外部条件时，停下来在任务文件末尾写“问题”一节并在看板标为「阻塞」，不要自行扩大范围或猜测产品规则。**
5. 运行完成检查（见下），全部通过后：
   - 在任务文件末尾追加“完成记录”：改动文件、迁移、接口、验证命令与结果、是否连过真实第三方引擎、遗留问题；
   - 在 `docs/development-status.md` 更新对应模块状态与验证记录；
   - 看板状态改为「待验收」。
6. 提交信息用 `T-xxx: 简述`。不要合入 main，不要 push --force；由开发负责人审查后合入。

## 完成检查（必须全部通过）

```bash
docker compose up -d                                  # 本地依赖（含 LiteLLM）
(cd backend && uvx ruff@0.14.0 check .)               # 后端静态检查
./scripts/test-postgres.sh                            # 迁移升降级 + 全量测试（含 PostgreSQL 专用测试）
docker compose exec web sh -c "npx tsc -b && npm run lint && npm run build"
docker compose exec api python /app/scripts/check_gateway_e2e.py   # 涉及模型/网关时
```

## 硬性规则

- **不得把 mock、占位页、前端本地状态或 Toast 记为完成。** 依赖第三方引擎而没有真实连通的部分，在完成记录中如实写明。
- 每个写操作都要：服务端权限检查、租户隔离、审计记录、测试（成功 / 校验失败 / 403 / 跨租户 404）。
- 新迁移 revision ID ≤ 32 字符，必须可降级；不改已有迁移。
- 新领域代码放在自己的文件：`backend/src/beework/models/<domain>.py`（在 `models/__init__.py` 末尾注册）、`schemas/<domain>.py`、`api/<domain>.py`（在 `api/__init__.py` 注册）、`frontend/src/pages/<domain>/`。
- 锁租户行用 `api.common.lock_tenant`，不要对 `tenants` 用裸 `with_for_update()`。
- 模型调用只经 LiteLLM，知识检索只经 RAGFlow，追踪只经 Langfuse；不要在 BeeWork 里自建这些能力，也不要读写它们的内部存储。
- 不提交任何真实密钥；不修改 `.env`。
- 不顺手重构与任务无关的代码；发现问题写进完成记录的“遗留问题”。

## 读取大文件

- 不要整篇读取 `docs/BeeWork-prototype.html`（约 1.3 MB 的打包单页应用）。用搜索按页面 ID 或界面文案定位，只读命中的片段。
- 不要整篇读取 `docs/BeeWork-PRD.md`（约 2200 行）。先用 `grep -n '^#' docs/BeeWork-PRD.md` 看章节目录，再按任务引用的章节或需求编号（如 3.9、AST-021）只读对应行范围。
- 其他超过约 500 行的文件同样处理：先搜索，再读需要的行。
