# T-006 顶栏环境切换与租户切换

**阶段**：2　**依赖**：T-001　**PRD**：GLB-003、GLB-021、GLB-087

## 范围

1. 顶栏环境切换（开发/预发/生产，GLB-021）：可选项来自租户套餐（非企业版只有生产，此时只展示不可切换）；当前环境保存在前端状态并通过请求头 `X-Environment` 传给后端；后端提供 `current_environment` 依赖（校验合法性与套餐），供后续工作台、渠道、运营数据按环境过滤。
2. 侧栏租户切换卡（GLB-003）：展示租户头像字母、名称、套餐名；多租户时下拉切换并回到工作台；`/auth/me` 的 membership 增加 `plan`。
3. 环境健康点：先按 LiteLLM 同步状态显示（synced 绿色、failed 红色、未配置灰色）。

## 验收标准

- [ ] 切换环境后，“模型与环境”页默认打开顶栏所选环境。
- [ ] 后端测试：非企业版传 `X-Environment: staging` 返回 403。

## 完成记录

**改动文件**
- 后端：`api/conventions.py`（`current_environment` 依赖）、`api/tenant_models.py`（`GET /tenant/environments`）、`api/common.py` 与 `schemas/__init__.py`（`/auth/me` 的 membership 增加 `plan`、`environments`）、`tests/test_environments.py`（6 项）。无迁移。
- 前端：新增 `components/EnvironmentSwitcher.tsx`；修改 `api.ts`（`X-Environment`）、`App.tsx`、`TenantModelSettingsPage.tsx`、`types.ts`、`common.json`、`App.css`。

**行为**
- `current_environment`：读 `X-Environment`（缺省生产，大小写不敏感）；不是三个环境之一返回 422 `environment.invalid`；套餐不提供返回 403 `environment.unavailable`（非企业版传 `staging` 即 403）。后续工作台、渠道、运营数据的接口依赖它即可按环境过滤；目前只有 `GET /tenant/environments` 使用。
- `GET /tenant/environments`：三个环境各自是否可用、调用者当前环境、网关同步状态（synced → 绿点、failed → 红点、其余 → 灰点）。
- 顶栏切换器：企业版可在 开发/预发/生产 间切换，其他套餐只显示“生产”不可切换；选择按租户保存在浏览器；每 60 秒后台刷新健康点；切换后整页路由重新挂载，使页面按新环境重新取数。
- 侧栏租户卡：头像字母、企业名、**套餐名**；多租户时下拉切换，切换后回到工作台。
- “模型与环境”页默认打开顶栏所选环境。

**验证**
- `ruff`、`./scripts/test-postgres.sh`（全量 287 项）、前端检查通过。覆盖：membership 带套餐与环境列表、企业版可选任意环境且默认生产、健康状态随网关同步状态、非企业版对 `staging`/`development` 403、未知环境 422、跨租户 403 与未登录 401。**没有在浏览器里目视检查页面。**

**遗留问题**
- 助理、运行等既有接口仍固定使用生产环境，尚未读取 `X-Environment`（需各自任务接入）；控制台的环境选择因此仍只显示生产。
