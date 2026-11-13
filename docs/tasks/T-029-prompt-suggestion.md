# T-029 助理提示词“AI 生成建议”

**版本**：V0.2　**依赖**：T-020、T-025　**PRD**：AST-021（AI 生成建议）、AST-052（“AI 已优化”标签）

## 范围

1. `POST /assistants/prompt-suggestions`：输入名称与使用场景（及可选的现有提示词），经 LiteLLM 用当前环境默认对话模型生成提示词（≤ 8000 字），`source=debug` 计点；超时 30 秒。
2. 向导第 2 步“AI 生成建议”按钮：生成中加载态；已有内容时先确认覆盖；生成结果只填入表单，用户保存后才写草稿。
3. 使用过建议并保存的草稿标记 `prompt_ai_optimized=true`，概览显示“AI 已优化”（AST-052）。
4. 生成用的系统提示词放在后端模板文件中（中英两版，按界面语言选择）。

## 验收标准

- [ ] 测试（假模型）：超长输出截断到 8000；名称或使用场景为空时 422；无编辑权限 403。

## 完成记录（2026-11-13，Tencent Hunyuan Hy）

**范围适配（验收请留意）**：规格第 2 项与 PRD AST-021 写「向导第 2 步」，但编辑向导在 T-021 重构时已删除（`docs/development-status.md`：新建只填名称、简介、组织、分类、模型，创建后直接进入智能体页补其余配置）。按钮落在当前等价位置——智能体「基础」页的系统指令字段旁；「AI 已优化」落在概览页新增的基础配置卡片（AST-052）。没有重建向导，也没有改动其它配置流程。

**改动文件**

- 后端：`api/prompt_suggestions.py`（新，`POST /assistants/prompt-suggestions`）、`prompts/prompt_suggestion.zh-CN.md` / `prompt_suggestion.en.md`（新，生成用系统提示词）、`runtime/llm.py`（`build_suggestion_model`）、`schemas/assistants.py`（`AssistantDraftConfig.prompt_ai_optimized` 与请求/响应模型）、`api/__init__.py`（注册路由）。
- 前端：`pages/assistants/AssistantBasicsPanel.tsx`、`AssistantDetailPage.tsx`、`types.ts`、`i18n/{zh-CN,en}/{assistants,detail}.json`、`App.css`。
- 测试：`backend/tests/test_prompt_suggestions.py`（11 项）。
- 文档：`docs/tasks/README.md`、`docs/development-status.md`、本文件。

**迁移**：无。`prompt_ai_optimized` 存在 `assistants.draft_config` JSON 内，旧草稿缺省 `false`；`alembic check` 无差异，版本快照 DSL 不受影响。

**接口**：`POST /api/v1/assistants/prompt-suggestions` — 权限 `assistants:edit` + CSRF，模型取当前环境（`X-Environment`）默认对话模型；请求 `name` / `scenario` / `current_prompt?` / `locale?` / `assistant_id?`，响应 `prompt` / `truncated` / `locale` / `environment` / `model_id`。计点 `source=debug`（1 点 `monthly_points`）、30 秒超时、结果去 Markdown 围栏并截断到 8000 字、成功写审计 `assistant.prompt_suggestion`。生成结果只填表单，不写草稿；用户保存时前端把 `prompt_ai_optimized` 写进草稿。`assistant_id` 可选，传入时做资源级编辑权限与租户隔离校验（跨租户 404）。

**测试覆盖**：成功（中文模板 / `source=debug` 计点 metadata / 审计 / 结果不落库）、英文模板按界面语言、8000 字截断与围栏剥离、现有提示词作为参考素材、422（名称或场景为空 / 空白 / 超长 / locale 非法）、403（viewer）、跨租户 404、无默认模型 422、网关密钥不可用 409、点数配额 403、超时 504、模型异常与空输出 502 且不写审计、草稿标记保存往返。

**验证命令与结果**

- `uvx ruff@0.14.0 check .`：`All checks passed`。
- `docker compose exec web sh -c "npx tsc -b && npm run lint && npm run build"`：EXIT=0；另跑 `npm run i18n:check`、`npm run style:check` 均通过（8 条既有 eslint warnings）。
- PostgreSQL 迁移升降级：`alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head` → `MIGRATE_EXIT=0`。
- `test_prompt_suggestions.py`：11 项 OK。
- 全量后端套件 `python -m unittest discover -s tests -v`（干净代码树 + 独立 PostgreSQL 库）跑完 **1248 项**，本任务 11 项全部 ok；另有 3 failures + 1 error 均与本次改动无关：3 项流式 / Dify 用例是因为隔离容器未接 Valkey（同轮还有 2 项 PostgreSQL 用例因 Valkey 不可达 skip），补齐 `VALKEY_URL` 后单独重跑这 3 项全部 ok；剩下 `test_retire_flow_canvas_migration.test_the_migration_can_be_downgraded_and_run_again` 在**纯 main 基线（f2978c7，不含本任务改动）**上同样失败（降到 0051 后 `alembic check` 与 head 模型对比返回 255）——本任务无新迁移、未改 SQLAlchemy 模型，干净树 head 上 `alembic check` 返回 0。

**是否连过真实第三方引擎**：**否**。本接口只经 LiteLLM 调用，不读写其内部存储；本次验收没有真实供应商 Key 可用，`check_gateway_e2e.py` 也未跑通（见下）。生成链路用假模型验证，不能据此认为真实模型已连通。

**遗留问题**

1. `check_gateway_e2e.py` 未实跑：共享 Compose 开发库的 `alembic_version` 被并行分支迁移 `0054_user_sidebar_preference` 推进，api 服务按标准启动命令（含 `alembic upgrade head`）失败；改用隔离库 + 「main + 本任务改动」干净代码树后，API 进程在 `from beework.main import app` 阶段持续阻塞（同一代码树的 `python -c "import beework.main"` 可通过），未能监听 8000。与 T-014 完成记录中记录的共享栈问题同类。
2. 完成检查在隔离环境执行：共享工作区同时存在其它任务的未提交改动（`billing.py`、迁移 `0054_billing.py` 等），会污染 ruff / 迁移 / 全量套件；最终验证使用 `git archive HEAD` + 本任务改动构造的干净代码树与独立一次性数据库 `beework_t029_suite` / `beework_t029_e2e`，标准脚本 `./scripts/test-postgres.sh` 因共享 api 服务未就绪而中断，改以等价命令逐条执行。
3. 审计描述 `assistant.prompt_suggestion` 在 `api/prompt_suggestions.py` 内用 `AUDIT_ACTION_DESCRIPTIONS.setdefault` 注册，未改共享的 `models/__init__.py`（该文件当时被并行任务修改中）。
4. AST-052 的「查看完整提示词」只读弹窗仍缺：T-021 重构后未落地，本任务规格第 3 点只要求「AI 已优化」标签，故概览卡片只显示标签与提示词前三行摘要。
5. 共享工作区在本次开发期间被并行任务切换过分支；本任务分支 `task/T-029-prompt-suggestion` 的提交只包含上列本任务文件，未包含其它任务的改动。
