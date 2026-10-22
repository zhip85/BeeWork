# T-001 API 公共约定

**阶段**：0　**依赖**：无　**PRD**：GLB-063、GLB-084、RUL-005、5.7、NFR-009

## 目标

在大量业务模块开工前，把每个模块都会重复用到的约定做成公共代码，避免各模型各写一套。

## 范围

后端新建 `backend/src/beework/api/conventions.py`（或拆成多个小文件），并改造现有代码使用它：

1. **错误格式**：`ApiError(status_code, code, detail)` 异常 + 全局异常处理器，响应 `{"detail": "中文信息", "code": "domain.reason"}`。现有 `HTTPException(detail="...")` 保持兼容（处理器为其补 `code: "error"`），不要求一次改完旧代码。
2. **分页**：`Pagination` 依赖（`limit` 默认 20、最大 100，`offset` ≥ 0）与泛型响应 `Page[T] = {items, total, limit, offset}`，提供 `paginate(db, statement)` 帮助函数。
3. **乐观锁**：`VersionedMixin`（`row_version` 列，int，默认 1）与 `check_row_version(obj, expected, db)`：不一致时返回 409，`code="conflict.stale"`，`detail="该对象已被 {最后修改人} 修改，请刷新后重试"`（需要 `updated_by` 列，一并放进 mixin）。
4. **通用列**：`TimestampMixin`（`created_at`、`updated_at`）、`TenantOwnedMixin`（`tenant_id` 外键 + 索引）、`SoftDeleteMixin`（`deleted_at`）。
5. **平台管理员依赖**：`require_platform_admin` 依赖，替换 `api/platform_*.py`、`api/audit.py`、`api/integrations.py` 中手写的 `if not user.is_platform_admin`。
6. **名称校验**：`normalize_name(value, max_len)`：去首尾空格、非空、长度；`ensure_unique_name(db, Model, tenant_id, name, exclude_id)`：同租户同类对象不区分大小写唯一（5.7）。
7. **URL 安全校验**：`validate_public_https_url(url)`：必须 https、主机名含点、解析后的所有 IP 不得是私网/回环/链路本地/保留地址（NFR-009 SSRF）。在 `PlatformModelWriteRequest.endpoint_url` 上启用（解析失败给出清晰错误；测试中用可注入的解析函数避免依赖 DNS）。
8. **资源范围授权**：`ensure_resource_in_scope(membership, resource, db)`：校验 `resource.tenant_id == membership.tenant_id`，若资源有 `organization_id` 则调用 `rbac.ensure_organization_scope`；不在范围时返回 **404**（不泄露存在性）。`scope_filter(statement, Model, membership, db)`：给列表查询追加组织范围过滤。

## 验收标准

- [ ] 上述工具都有单元测试（含 409 冲突、分页边界、URL 拒绝 `https://127.0.0.1`、`https://10.0.0.1`、`http://example.com`、解析到私网 IP 的域名）。
- [ ] 平台接口全部改用 `require_platform_admin`，原有测试仍通过。
- [ ] `docs/architecture.md` 第 3.2 节错误格式、分页等说明更新为指向本模块的实际用法示例。
- [ ] 架构文档中给出一个“新领域模块样板”代码片段（模型 + schema + 路由 + 测试骨架），后续任务照抄。

## 不在范围

- 不重写现有所有接口的错误信息与分页格式（旧审计接口的 `events` 字段保持不变）。
