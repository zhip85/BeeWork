# T-014 模型目录补全：降级、停用影响、删除限制、测试调用

**版本**：V0.2　**依赖**：T-005、T-070　**PRD**：3.14.2、PLT-090–093、NFR-004

## 第 0 步：实测（结果写在本文件末尾“实测记录”）

用本地 LiteLLM 确认降级的实现方式：请求级 `fallbacks` 参数，还是 deployment 级 / router 级配置；能否通过 API 动态设置（不改 `config.yaml`）。选择**可经 API 管理**的方式。

## 范围

1. 先核对现有 `platform_models` 字段与 PRD 3.14.2 的差异（区域、上下文窗口、最大输出、默认温度、RPM、降级模型、输入/输出扣点），缺的补迁移。
2. 降级模型：下拉只列同类型、同区域的其他非停用模型；保存时检测循环引用；同步到 LiteLLM（按第 0 步结论）；运行时失败或超时按降级模型重试一次（PLT-092）。
3. 停用影响（PLT-090）：模型停用后，引用它的助理/智能体（T-070 引用服务）在运行时自动使用降级模型，在配置处标红「模型已停用，正在使用降级模型 X」；没有降级模型时配置检查报阻断问题。
4. 删除（PLT-093）：有租户启用的模型不可删除，只能停用；删除时清理 LiteLLM deployment。
5. 测试调用（PLT-091）：编辑弹窗中“测试调用”返回延迟与示例输出（复用现有连通性测试，增加示例输出展示）。

## 验收标准

- [x] 测试：循环降级被拒；跨区域降级被拒；有租户启用时删除 409（`test_model_catalog_completion.py` 11 项）。
- [x] e2e：主模型 deployment 指向不可达地址时，请求由降级模型完成（`mock_response` deployment）；证据为下方「实测记录」在真实 LiteLLM 上的实测，`check_model_fallback_e2e.py` 未实跑（见「完成记录」遗留问题）。

## 实测记录（第 0 步，2026-11-13）

环境：本地 Compose 的 LiteLLM（`beework-litellm-1`，配置 `model_list: []` + `store_model_in_db: true`），用平台 master key 直接打 HTTP API。临时建两个 deployment：主模型 `bw-t014-probe-primary`（`openai/gpt-4o-mini` + `api_base=http://127.0.0.1:9/v1`，不可达）、降级模型 `bw-t014-probe-fallback`（`openai/gpt-4o-mini` + `mock_response=T014_FALLBACK_OK`）。

| 探测 | 结果 |
|---|---|
| 请求级 `POST /chat/completions` 带 `fallbacks: ["bw-…"]` | HTTP 200，返回降级模型内容 `T014_FALLBACK_OK`。无需改 `config.yaml` |
| `POST /fallback`（`model` / `fallback_models` / `fallback_type=general`） | HTTP 200；`GET /fallback/{model}` 读回一致；`GET /router/settings` 的 `current_values.fallbacks` 可见；`DELETE /fallback/{model}?fallback_type=general` 可删除。均为 API 动态管理，无需改 `config.yaml`（重启后是否保留未验证） |
| 仅靠 router 级配置、请求不带 `fallbacks` | 主模型连接失败后自动由降级模型完成（HTTP 200） |
| 租户虚拟 Key（team 只放行主模型）触发到未放行模型的降级 | HTTP 200：LiteLLM 在 router 层降级时不重做 Key 的模型鉴权，因此 BeeWork 不必把降级模型加入 team 白名单；降级模型只能由平台目录配置 |

结论：deployment 本身没有降级字段，`/model/new` 的 `litellm_params` 不接受降级配置；降级是 **router 级**配置，且 `POST /fallback` / `GET /fallback/{model}` / `DELETE /fallback/{model}` 可经 API 动态管理。本任务选 **router 级 `POST /fallback` 作为「同步到 LiteLLM」的方式**（保存模型时入队同步），运行时再把同一降级模型通过请求级 `fallbacks` 传给网关作为双保险（网关重启丢失 router 配置时仍能降级，且租户无法指定该参数）。

清理：两个临时 deployment、fallback 配置、临时 team / 虚拟 Key 均已通过 API 删除，`/v1/models` 中不再有 `t014` 字样。

## 完成记录（2026-11-13）

### 字段核对（第 1 步）

迁移 `0009_platform_models` 已包含 PRD 3.14.2 的全部字段：`region`、`context_window`、`max_output_tokens`、`default_temperature`、`rpm_limit`、`fallback_model_id`、`input_points_per_thousand` / `output_points_per_thousand`（扣点分别设置）。**无字段差异，因此没有新迁移、没有数据变更**；状态仍为 `healthy` / `canary` / `disabled`。

### 改动文件

- 新增：`backend/src/beework/model_catalog.py`（降级校验、删除守卫、运行时别名解析、目录状态）、`backend/tests/test_model_catalog_completion.py`、`scripts/check_model_fallback_e2e.py`、`frontend/src/i18n/{zh-CN,en}/modelCatalog.json`。
- 后端：`api/platform_models.py`（降级校验改走领域服务、新增 DELETE、状态变更后重同步引用方）、`integration/litellm.py`（`set_fallback` / `clear_fallback`）、`gateway_sync.py`（同步 router fallback、新增 `gateway.delete_model` 作业）、`api/assistants.py` + `schemas/assistants.py`（模型选项返回停用状态与降级模型名）、`assistant_checks.py`（停用标红 / 无降级阻断）、`runtime/llm.py` + `runtime/executor.py`（运行时解析与请求级降级）、`models/__init__.py`（审计动作）。
- 前端：`pages/platform/PlatformModelsPage.tsx`（编辑弹窗测试调用、删除确认）、`pages/assistants/AssistantBasicsPanel.tsx` + `types.ts`（配置处标红）、`i18n/index.ts`（注册命名空间）。
- 文档：本文件、`docs/development-status.md`、`docs/tasks/README.md`。

### 接口

- 新增 `DELETE /api/v1/platform/models/{model_id}`（平台管理员 + CSRF；409 `model_catalog.in_use`；审计 `model_catalog.deleted`；入队 `gateway.delete_model` 清理 deployment 与 router fallback）。
- 新增 Worker 作业 `gateway.delete_model`；`gateway.sync_model` 同步时调用 `POST /fallback` 或 `DELETE /fallback/{alias}`。
- `PUT /api/v1/platform/models/{model_id}`：降级目标追加「同类型、同区域、未停用、无循环（含历史循环 409）」校验；状态/降级变更时重同步把它设为降级的模型。
- `GET /api/v1/assistants/model-options`：选项新增 `status`、`fallback_display_name`；`GET /assistants/{id}/checks` 新增 `runtime.model_disabled_fallback`（high）/ `runtime.model_disabled`（blocker）。
- `POST /api/v1/platform/models/{id}/test` 未改动，编辑弹窗直接复用（返回延迟与示例输出）。

### 验证

| 检查 | 结果 |
|---|---|
| `uvx ruff@0.14.0 check .`（backend 全量） | All checks passed |
| `test_model_catalog_completion.py`（本地 Python 3.12 venv） | 21 项通过：循环 / 跨区域 / 跨类型 / 停用目标 / 自身 / 不存在目标 / 合法长链，删除 409 / 204 + 审计 + 入队 / 403 / 无 CSRF 403，运行时解析（含已删除不记降级），停用目标不挡无关编辑，区域变更重校验引用方，传递链重同步，配置检查（单一故障 + 编排模型），模型选项，LiteLLM client 契约，网关同步与清理作业，租户 team 白名单含解析别名，改写模型目录解析 |
| PostgreSQL 一次性库 `beework_t014_test`：`alembic upgrade head → alembic check → downgrade base → upgrade head` + 全量 `unittest discover -s tests -v` | 迁移四步通过；全量 1274 项中 2 个失败——`test_retire_flow_canvas_migration` 复跑通过（共享库竞争的环境噪音），`test_tenant_lifecycle` 暴露了真实缺陷（无降级模型同步误调 `clear_fallback`，已修，见「Review 修复记录」） |
| 全量后端套件（修复后重跑，本地 SQLite）：`python -m unittest discover -s tests` | **1285 项全部 OK（24 项既有跳过），EXIT 0** |
| 针对性回归（本地 SQLite）：`test_integrations` / `test_tenant_auth` / `test_versions` / `test_runtime` / `test_agent_runtime` / `test_intent_config` / `test_intent_engine` / `test_worker` / `test_tenant_lifecycle` | 159 项 + 22 项全部 OK |
| 前端 `npx tsc -b` / `npm run lint` / `npm run build` / `npm run i18n:check` / `npm run style:check`（宿主机） | 全部通过（lint 0 errors、8 条既有 warnings；i18n 24 个命名空间；font size 检查通过） |
| 真实 LiteLLM 降级（不可达主 deployment + `mock_response` 降级 deployment） | 通过，见上文「实测记录」 |

### 是否连过真实第三方引擎

**是**：本地 Compose 的 LiteLLM 1.106.0，使用平台 master key 与租户虚拟 Key 通过真实 HTTP API 验证了请求级 / router 级降级；没有使用真实供应商 API Key（降级 deployment 用 `mock_response`，规格允许）。

### Review 修复记录（2026-11-13 第二轮）

1. **停用模型后直连降级会被租户 Key 拒绝**（正确性）：`sync_tenant_environment` 的 team 白名单不再只列租户启用且未停用的模型，而是逐个选中模型走 `resolve_runtime_model`，把运行时解析出的别名（停用模型的替代模型、在用模型的请求级降级目标）一并放进白名单；禁用且无可用降级的模型不再占位。降级链上任一模型变化时，通过新增的 `models_whose_chain_includes`（传递闭包）重同步所有相关环境的 team。
2. **停用的降级目标挡住无关编辑**：`PUT /platform/models/{id}` 只在 `fallback_model_id` 真的变化时执行降级校验；改 RPM / 价格不再因旧目标已停用而 422。
3. **删除竞态 500 → 409**：入队清理作业（会触发 flush 命中降级外键）移入 `try`，与租户启用的竞态统一返回 409 `model_catalog.in_use`。
4. **改降级目标后重校验引用方**：修改模型的区域或类型时，若有其他模型降级指向它则 422（提示先调整其降级配置）；状态/降级变化时对整条引用链（传递闭包）重投影 router fallback 并重算相关环境白名单，A→B→C 中 C 停用后 A 的降级不再指向网关已删除的 deployment。
5. **改写与意图模型走目录解析**：`gateway_rewrite_llm` / `gateway_intent_llm` 对配置的模型别名先做 `resolve_runtime_model`（停用→替代模型），`build_rewrite_model` / `build_intent_model` 支持请求级 `fallbacks`，并在 metadata 记录 `model_fallback_from`；配置检查新增 `runtime.rewrite_model_disabled` / `runtime.intent_model_disabled`（仅在对应功能开启时检查）。
6. **链长误判为循环**：`_chain` 去掉深度上限，仅以「重复节点」判定环（`seen` 集合本身保证终止），合法长链不再 409。
7. **同一故障只报一条**：配置检查以目录状态为单一来源——停用/删除按降级有无报 `model_disabled_fallback`（high）或 `model_disabled`（blocker），仅未进环境白名单的在用模型才报 `model_unavailable`。
8. **模型选项 N+1**：新增 `model_states` 批量接口（一次取目录、内存走链），`/assistants/model-options` 不再逐项查库。
9. **已删除模型不再伪造降级记录**：`resolve_runtime_model` 对已删除别名不再返回 `switched_from="已删除模型"`，运行记录与用量归因不会出现未发生的降级。

新增/调整测试：`test_model_catalog_completion.py` 11 → 21 项（长链、无关编辑、区域变更重校验、传递链重同步、已删除解析、单一故障、编排模型检查、team 白名单、改写模型解析、短调用降级契约）；`test_tenant_lifecycle.py` 的 FakeLiteLLM 补齐 `set_fallback` / `clear_fallback` 契约（该处暴露了修复 9 相关的真实缺陷：无降级模型同步时误调用 `clear_fallback` 导致作业失败，已由契约补齐覆盖）。

e2e 脚本：`check_model_fallback_e2e.py` 在仅网关模式加入**租户虚拟 Key 场景**——按 BeeWork 现在同步的白名单建 team（主模型 + 解析出的降级别名）并生成 Key，验证租户 Key 能直连降级别名、不能访问白名单外模型、请求级降级在租户 Key 下可用（旧脚本全程用 master key，绕过了 team 白名单，测不出第 1 项）。

### 遗留问题

- `POST /fallback` 写入的 router 设置在 LiteLLM 重启后是否持久化未验证；运行时请求级 `fallbacks` 已作为双保险，不受其影响。
- 删除限制按规格只检查「有租户启用」与「被其它模型设为降级目标」；未扩展 T-070 `resource_references` 写模型引用边，因此草稿里仍引用某模型的智能体不会阻止删除（删除后配置检查按「已删除」报阻断，运行时不会自动找到降级模型）。
- 编辑弹窗的「测试调用」使用**已保存**的配置，未提交的表单修改不参与测试（弹窗内已注明）。
- `scripts/check_model_fallback_e2e.py` 的完整模式（BeeWork API + Worker）与 `scripts/check_gateway_e2e.py` 未实跑：验收期间共享 Compose 栈的 api 服务未监听 8000（api 容器被并行任务用作 `test-postgres` 长跑容器），LiteLLM 重启后持续不健康——容器内 Python 导入在宿主机高负载下慢一个数量级，连新起的独立实例也长时间无响应；脚本本身已按 review 扩展（租户 Key + team 白名单场景），脚本侧仅网关模式与完整模式都待环境恢复后实跑。降级行为本身已由「实测记录」在真实 LiteLLM 上验证。容器内 `docker compose exec web ...` 与 `./scripts/test-postgres.sh` 原命令同样受此影响，改用宿主机等价命令。
- 环境空闲后请重跑 `./scripts/test-postgres.sh`、`docker compose exec web sh -c "npx tsc -b && npm run lint && npm run build"`、`docker compose exec api python /app/scripts/check_gateway_e2e.py` 与 `scripts/check_model_fallback_e2e.py`（完整模式），以补齐全量汇总与 BeeWork API → Worker → LiteLLM 的联动证据。一次性库 `beework_t014_test` 由后台测试占用，未删除。
- 按 `docs/development-status.md` §6「未经用户要求不提交」，本次改动留在工作区，未提交到分支。
