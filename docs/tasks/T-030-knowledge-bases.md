# T-030 知识库与 RAGFlow dataset

**阶段**：4　**依赖**：T-001、T-002　**PRD**：3.4.1、3.4.2、3.4.3 设置（KNW-032）、3.4.4、4.6、RUL-011–013
**外部条件**：RAGFlow API Key（RAGFlow Cloud 付费版 Starter/Pro 才提供 API Key，免费版没有）

## 第 0 步：对接验证（先做，结果写进本文件末尾的“实测记录”）

`integrations/ragflow.py` 目前只按自托管 RAGFlow 的 `/api/v1` 文档写成，并且**只有契约测试**。拿到 Key 后先确认：

1. RAGFlow Cloud 的 API 地址与自托管是否一致（`{base}/api/v1/datasets` 等），鉴权方式是否为 `Bearer`。
2. 云端可用的 Embedding 模型列表与默认值；数据集数量、文件大小、存储等套餐限制（Starter 标注“50 Apps”，需确认 dataset 是否计入）。
3. 数据存储区域（影响 NFR-007 数据驻留，结论报给开发负责人）。
4. `chunk_method` 可选值及参数（PRD 的 Semantic / Fixed 512 / By heading 如何映射）、`qa` 方法如何写入问答对（上传文件还是逐条 add chunk）。

把客户端中与实测不符的地方改掉，并补对应契约测试。

> 架构约束：RAGFlow Cloud 无法访问本机 Compose 里的 LiteLLM，所以云端方案下 Embedding 使用 RAGFlow 自带模型，Embedding 用量不经 BeeWork 网关计量；自托管 RAGFlow 时再改为指向 LiteLLM 的 OpenAI 兼容接口。

## 数据模型（`models/knowledge.py`）

`knowledge_bases`：按 PRD 4.6；另加 `ragflow_dataset_id`（文档数据集）、`ragflow_faq_dataset_id`（FAQ 数据集，`qa` 方法）、`sync_status`、`sync_error`、`row_version`、`deleted_at`。普通库两个 dataset，FAQ 库只有 FAQ dataset。

## 行为

- 创建知识库：BeeWork 先落库（状态草稿）→ 入队 `knowledge.ensure_datasets` 创建 RAGFlow dataset（名称用 `bw-{tenant}-{kb_id}`，不用用户输入的名称，避免冲突与泄露）。
- 修改向量模型或切片策略：RAGFlow 一般不允许已有分片的 dataset 改 Embedding 模型，按“新建 dataset → 全部文档重新上传解析 → 切换映射 → 删除旧 dataset”的重建任务实现，期间已挂载的助理继续用旧 dataset（3.4.4）。
- 删除知识库：软删除 → 入队删除 RAGFlow dataset；被助理引用时确认文案带引用数（KNW-042），引用处显示失效。
- 状态派生规则按 3.4.4。
- 权限：`knowledge:*`；组织范围过滤；写审计。

## 界面

知识库列表（KNW-001–005）、创建页（3.4.2）、详情框架与设置视图（KNW-020–022、KNW-032）。文档与 FAQ 页签由 T-031、T-032 实现。

## 验收标准

- [ ] RAGFlow 未配置时创建知识库仍成功，`sync_status=not_configured`，界面提示管理员配置。
- [ ] 跨租户访问 404；查看者只读。
- [ ] 实测记录已填写；用真实 RAGFlow 跑通创建与删除 dataset。

## 完成记录（用假 RAGFlow 开发，第 0 步未做）

**重要前提**：没有 RAGFlow Key，也没有自托管实例，所以本任务是对着 `scripts/fake_ragflow.py`（只实现数据集接口、按 RAGFlow 文档写的假服务）开发和验证的。**“第 0 步：对接验证”和验收项“用真实 RAGFlow 跑通创建与删除 dataset”都没有做**，“实测记录”一节留空。通过假服务只能证明 BeeWork 按文档契约发请求、处理结果的逻辑正确，不能证明真实 RAGFlow 是这样响应的。

**改动文件**
- 后端：新增 `models/knowledge.py`、迁移 `0029_knowledge_bases`（可降级）、`knowledge.py`（派生状态、数据集同步、Worker 作业）、`schemas/knowledge.py`、`api/knowledge.py`、`tests/test_knowledge.py`（19 项，起一个真实的假 RAGFlow HTTP 服务）；修改 `integrations/ragflow.py`（`parser_config`、`find_dataset`）、`worker/scheduler.py`、`worker/runner.py`。
- 工具：`scripts/fake_ragflow.py`（标准库实现的假服务，带失败注入）、`scripts/check_knowledge_e2e.py`、`compose.yaml` 的 `ragflow-fake` 服务（profile `fake-ragflow`，默认不启动）。用法：`RAGFLOW_BASE_URL=http://ragflow-fake:9380 RAGFLOW_API_KEY=fake-ragflow-key docker compose --profile fake-ragflow up -d ragflow-fake api worker`。
- 前端：`pages/knowledge/`（列表、创建页、详情 + 设置视图）、`i18n/*/knowledge.json`、`App.tsx` 路由、`App.css`。

**行为**
- 接口：`GET/POST /knowledge-bases`、`GET/PUT/DELETE /knowledge-bases/{id}`、`POST …/copy`、`POST …/retry-sync`、`GET …/references`。权限 `knowledge:view/edit/delete`；租户范围角色看全部，组织范围角色只看自己组织子树，助理范围角色看不到；跨租户 404。名称 ≤ 50 字、租户内不分大小写唯一；创建默认 bge-m3 / Semantic / 0.62 / 草稿。
- 创建只落库并排队 `knowledge.ensure_datasets`，Worker 创建数据集：普通库两个（文档 + FAQ，`qa`），FAQ 库只有 FAQ 数据集；数据集名只用 ID（含代数 `-g1`），不用用户输入；重试先按名字查找，复用上次半途创建的数据集。RAGFlow 未配置时创建照常成功，`sync_status=not_configured`，页面提示；每小时 `knowledge.reconcile` 会为 failed / not_configured / pending 的库补同步，也可手动“重试同步”。
- 修改向量模型或切片策略：有文档时必须带 `confirm_reindex`，否则 409；保存后进入“处理中”，Worker 新建下一代数据集 → 运行 `REINDEX_HOOKS`（T-031 注册，重传并解析文档）→ 切换 → 再排队删除旧数据集；期间旧数据集继续服务，重建失败时保持旧索引并标记 failed。
- 切换类型：有文档不能切到 FAQ（409）；切到 FAQ 删除空的文档数据集，切回普通库重新创建。
- 状态由内容派生（草稿/处理中/已索引）：文档与 FAQ 的统计通过 `register_content_provider` 接入，T-031 / T-032 注册；删除前的引用数通过 `REFERENCE_COUNTERS` 接入，T-033 注册。目前文档数、FAQ 数、引用数都是 0。
- 删除：软删除，排队删除数据集（引擎不可用时作业重试，不丢）；复制：复制配置，状态草稿，新数据集；复制文档与 FAQ 待 T-031 / T-032。写审计（`knowledge.*`）。

**验证**
- ruff；`test_knowledge.py` 19 项通过（创建与同步、未配置、失败与重试、半途创建复用、唯一名与校验、权限与租户隔离、组织范围、设置校验与乐观锁、重建与失败回退、类型切换、状态派生、列表筛选、复制、删除与重试）。
- 真实栈端到端 `check_knowledge_e2e.py`（真实 API + Worker + 假 RAGFlow 容器）通过：创建、数据集同步、改设置触发重建并删除旧数据集、复制、删除。
- 写测试时发现并修复一个真实缺陷：重建时新数据集与旧数据集同名，按名字查找会把正在服务的旧数据集当成新的，切换后再把它删掉；数据集名现在带代数。

**遗留问题 / 待真实 RAGFlow 确认**
- `CHUNK_METHODS`（Semantic→`naive`、Fixed 512→`naive`+`chunk_token_num`、By heading→`book`）与 Embedding 模型名（`bge-m3` 等）的传法，都是按文档推断，未验证；`find_dataset` 对“名字没匹配到”的两种可能响应（空列表 / “don't own the dataset”错误）都做了处理。
- Embedding 选项目前是固定三项；正式版应来自租户已启用的 Embedding 模型。
- KNW-041（内容变更审批）、文档/FAQ 页签、检索属 T-031 / T-032 / T-033；详情页的页签只是占位。
- 页面未做浏览器目视验收。

**审查修正**
- 修复删除知识库与重建/创建并发时遗留孤儿数据集：此前删除只知道旧数据集，若 Worker 正好在重建或创建，新建的数据集永远没人删。现在重建切换前、创建完成后都会重新读取，发现已删除就把新建的数据集交给删除作业。
- 删除作业改为可重复执行：此前只要有一个数据集已被别的作业删掉，整个批次就报错重试，剩下的永远删不掉；现在对“已不存在”逐个跳过。（“数据集已不存在”的判断依据是假服务模拟的 RAGFlow 报错文案，真实 RAGFlow 的文案待确认。）
- 修复状态筛选的分页：此前先分页再按状态过滤，`total` 是未过滤的总数；现在先过滤再分页。
- 改名加租户级锁，两个并发改名不会取到同一个名字。
- 已知未处理：设置连续修改时，较早的一次重建完成后会短暂把“重建中”标志清掉，随后排队的下一次重建会收敛到最终配置。

## 知识引擎改为 RAGFlow / Dify 可配置（2026-11-05）

- **配置**：`KNOWLEDGE_ENGINE=ragflow|dify` 决定**新建**知识库用哪个引擎；`RAGFLOW_BASE_URL/RAGFLOW_API_KEY`、`DIFY_BASE_URL/DIFY_API_KEY` 分别配置。每个知识库在创建时记下自己的引擎（`knowledge_bases.engine`），之后改默认引擎不会让已有知识库的数据集变成孤儿，也不会自动迁移。Dify 的 Key 是工作区的“知识库 API”密钥（`dataset-…`），Dify Cloud 的地址是 `https://api.dify.ai/v1`。
- **代码**：新增 `integrations/knowledge_engine.py`（统一接口 `create_knowledge_dataset / find_dataset / delete_datasets`、`default_engine / engine_configured / open_engine`）与 `integrations/dify.py`；`RAGFlowClient` 实现同一接口，切片映射表 `CHUNK_METHODS` 挪进 RAGFlow 客户端；`knowledge.py` 只依赖接口。迁移 `0030_knowledge_engine`：数据集列改名为 `engine_dataset_id / engine_faq_dataset_id`，新增 `engine`（存量默认 `ragflow`）。数据集名改为 `bw-<知识库 id 前 24 位十六进制>-g<代数>[-faq]`（≤ 40 字符，满足 Dify 的名称上限）。平台“集成”列表里 RAGFlow 与 Dify 各一行。
- **Dify 与 RAGFlow 的差异（按 Dify 文档）**：知识库名 ≤ 40 字符；切片规则（`process_rule`）和问答模式（`doc_form`）是**文档级**参数，数据集创建时不带，留给 T-031 / T-032 上传文档时带；Embedding 在数据集上用 `embedding_model_provider` + `embedding_model`，BeeWork 的 `text-embedding-3-large`、`cohere-embed-v3` 映射到 Dify 云端的 OpenAI / Cohere 模型，`bge-m3` 云端没有，用工作区默认；Dify 没有批量删除，逐个删除，404 当作已删除。
- **验证**：`test_knowledge.py` 29 项（RAGFlow 23 项 + Dify 6 项：创建与命名、Embedding 映射、重建与可重复删除、改默认引擎后旧知识库保持原引擎、未配置与补同步、客户端契约）通过；`check_knowledge_e2e.py` 在真实 API + Worker + 假服务上分别用 RAGFlow 与 Dify 两种配置通过。假服务 `scripts/fake_ragflow.py` 现在同时模拟两种接口（Dify 在 `/v1`）。
- **用真实 Dify 的验证没有完成**：用你提供的 Dify Cloud Key 只读探测 `GET /v1/datasets`，返回 `403 forbidden：The API key is not authorized to access this knowledge base`；同一接口用乱写的 Key 返回 `401 Access token is invalid`，说明 Dify 认得这个 Key，但拒绝它访问知识库 API。可能是 Key 的创建者或套餐权限的限制，需要在 Dify 控制台确认。所以 Dify 客户端同样只对着假服务验证过，创建/删除/检索字段名和行为均按文档推断。

