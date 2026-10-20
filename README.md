# BeeWork

BeeWork 企业级 AI SaaS 平台开发骨架。当前 Docker Compose 环境包含 React/TypeScript 前端、FastAPI、PostgreSQL、Valkey，以及用于验证运行时依赖的最小 LangGraph 示例。当前开发阶段已加入企业租户、账号登录、工作区成员角色和审计基础。

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

首次启动时，API 会运行 Alembic 数据库迁移，并在开发环境创建本地企业和管理员账号：

```text
邮箱：admin@beework.dev
密码：Beework-Dev-2026!
```

这些凭据仅用于本地开发。生产环境必须关闭开发引导（`ENVIRONMENT` 设为 `production`）、设置至少 32 位随机 `AUTH_SECRET`、配置 `APP_ORIGIN` 为实际前端源，并启用 `AUTH_COOKIE_SECURE=true`；还应通过安全渠道建立生产管理员账号。

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

运行后端单元测试：

```bash
docker compose exec api python -m unittest discover -s /app/tests
```

身份与租户 API：

- `POST /api/v1/auth/login`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me`
- `GET /api/v1/tenants`：当前用户加入的企业
- `GET /api/v1/members`：列出选定企业成员（`X-Tenant-ID`）
- `PATCH /api/v1/members/{user_id}/role`：企业管理员调整成员角色
- `GET/POST /api/v1/platform/tenants`：平台管理员查看/创建企业

浏览器会话使用 HttpOnly Cookie；状态变更请求还需 `X-CSRF-Token`。租户 API 根据成员关系和角色校验访问权限，不能仅凭客户端传来的租户 ID 获得访问权限。

当前还没有成员邀请/停用、密码重置、SSO/MFA 或完整操作审计查询页面；目前完成的是企业与账号关系、角色管理、关键变更写入审计表的第一版基础。

停止容器但保留数据库卷：

```bash
docker compose down
```

本地数据库和 Valkey 端口只绑定到 `127.0.0.1`。当前数据库密码默认值仅适用于本地开发；生产环境必须使用密钥管理和专用部署配置。

前端开发依赖 React、React Router、React Flow、Formily 和 Lucide；模块页面当前使用开发期示例数据，后续逐步接入 API。
