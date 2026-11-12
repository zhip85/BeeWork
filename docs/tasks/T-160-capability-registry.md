# T-160 统一能力注册中心

**版本**：V0.5（运行时架构 v2）　**依赖**：T-070、T-072、T-073、T-074、T-075　**PRD**：演进 PRD 23.2、7.1、7.6；`architecture-runtime-v2.md`

## 目标

L2 直达与 L3 规划共用一份“能力资产”：每项能力有稳定的注册 ID、输入 Schema、意图描述与示例、权限、超时与重试策略，并能按智能体的挂载范围被**召回**。不再让每个调度器各自从 MCP / API / Skill / Dify 的表里拼工具清单。

## 设计

- **注册项**（`capability_entries`，工具级）：`id`（稳定 UUID）、`tenant_id`、`kind`（`mcp_tool` / `api_tool` / `skill` / `dify_workflow` / `dify_chatflow`）、`source_type` + `source_id`（所属连接 / 插件 / Skill / 流程连接的 ID）、`identifier`（工具名 / 接口名 / Skill 名 / 应用标识）、`name`、`description`、`intent_description`（≤ 500 字，说明“用户想做什么时该用它”）、`intent_examples`（≤ 10 条示例问法）、`input_schema`、`writes`、`direct_output`（路径 A 的结果是否原样返回）、`status`（available / disabled / invalid，随来源而定）、`timeout_ms`、`retry`（`max_attempts`、`backoff_ms`、`retry_on`），`row_version`、审计字段。
- **不合并来源表**：注册项是**派生层**。来源（MCP 测试成功并勾选、API 插件解析并勾选、Skill 通过检查、Dify 连接保存 / 读取参数）变化时在同一事务里重算对应注册项；删除 / 停用来源即让注册项失效。来源表保持原样，注册 ID 不随重算改变（按 `source_id + identifier` 对账）。
- **范围**：一个智能体可见的注册项 = 它已挂载（并勾选）的来源展开的注册项。叶子智能体不在注册中心（它是 L3 的委派工具）；知识库不是可直达的能力。
- **召回**（`registry.recall(db, tenant_id, assistant_id, query, k, history)`）：在可见注册项里按名称、说明、意图描述与示例召回前 k 个，返回注册项与得分。首版用 PostgreSQL 全文检索 + 关键词命中加权（与记忆检索同一套工具），接口里留出向量召回的替换点（不在本任务做向量）。
- **策略默认值**：超时与重试的解析顺序 = 注册项 → 租户默认 → 平台默认（30 秒、不重试）。本任务只存储与解析，**执行侧使用在 T-165**。

## 范围

1. 数据模型、迁移（可降级）、对账与失效逻辑；对已有 MCP / API / Skill / 流程连接做一次回填。
2. 接口（权限 `capabilities:view/edit`）：`GET /registry/entries?kind=&source_id=&q=`（租户内）、`PUT /registry/entries/{id}`（改意图描述与示例、`direct_output`、超时与重试，需 `row_version`）、`GET /assistants/{id}/registry/recall?q=&k=`（只看该智能体挂载范围内，供调试与 T-161 / T-162 复用）。
3. 前端：MCP 工具勾选列表、API 插件工具列表、Dify 连接页、Skill 详情里，每个工具 / 接口增加“意图描述与示例”“超时与重试”编辑；一个只读的“注册中心”视图（能力页新页签或设置里）列出所有注册项、来源与状态。
4. `architecture.md` 记录“注册项是派生层、挂载即授权范围”。

## 验收标准

- [ ] 测试：来源变化后注册项对账（新增 / 保留 ID / 失效 / 重新可用）；跨租户不可见；召回只返回该智能体挂载范围内的项；停用来源后召回不再返回；超时与重试默认值解析顺序。
- [ ] 测试：写操作标记（`writes`）与来源一致；`PUT` 校验 Schema 无关字段不可改（来源决定 `input_schema`）。
- [ ] 回填后既有挂载的 MCP / API / Skill / 流程连接都有注册项。

## 不在范围

- 向量召回与 embedding 基础设施。
- 执行侧的超时 / 重试 / 熔断（T-165）。
- Dify 应用参数的读取（T-163 提供 `input_schema`，本任务先用来源已有的映射）。
