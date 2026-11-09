# T-031 文档上传、解析状态同步、删除与重建

**阶段**：4　**依赖**：T-030　**PRD**：KNW-030、4.6 Document、NFR-003、NFR-009、NFR-020

> **知识引擎（2026-11-09 决定）**：本任务及知识相关任务一律使用 **Dify**；RAGFlow 仅保留为可选引擎实现、不再验证。下文中的“RAGFlow”按“知识引擎（Dify）”理解，历史完成记录保持原样。

## 范围

1. 上传：本地文件（PDF、DOCX、HTML、Markdown、TXT，单文件 ≤ 50 MB，一次 ≤ 20 个）与网页 URL（URL 用 T-001 的公网 HTTPS 校验）。服务端校验扩展名 + MIME 嗅探 + 大小；存储配额检查（T-010 `storage_gb`）。
2. 文件转交：API 接收后写 `documents`（状态处理中）并把文件暂存（本地卷或对象存储，路径带 tenant_id），入队 `knowledge.upload_document` → 上传到 RAGFlow → `start_parsing` → 入队轮询 `knowledge.poll_document`（指数退避，直到 DONE/FAIL）→ 回写分片数、状态、失败原因；成功后删除暂存文件（原文件下载改为从 RAGFlow 获取，若 RAGFlow 不支持则保留原文件并说明）。
3. 同名文件：提示覆盖 / 保留两者。
4. 行操作：详情 + 分片预览（调用 RAGFlow 分片列表接口）、重新索引、下载原文件、删除（BeeWork 软删除 + 入队删除 RAGFlow 文档）。
5. 列表：格式、分片数、负责人、状态计数筛选、搜索；知识库卡片计数同步。

## 验收标准

- [ ] 上传校验测试（超大、伪造扩展名、内网 URL 被拒）。
- [ ] 轮询任务幂等，RAGFlow 失败状态能在界面看到原因并可重试。
- [ ] 用真实 RAGFlow 上传一份 PDF 走完“处理中 → 已索引”，记录耗时（对照 NFR-003：10 MB ≤ 5 分钟）。

## 完成记录（对着假服务开发，真实引擎未验证）

**重要前提**：和 T-030 一样，本任务是对着 `scripts/fake_ragflow.py`（现在同时模拟 RAGFlow 与 Dify 的数据集 + 文档接口）开发和验证的。**验收项“用真实 RAGFlow 上传一份 PDF 走完处理中 → 已索引并记录耗时”没有做**；Dify 的文档接口字段（`create-by-file`、`indexing-status`、`segments`、`process_rule`）按文档推断，未在真实账号上验证（上传会消耗该工作区的 Embedding 额度，需先征得同意）。耗时（NFR-003）因此没有数据。

**改动文件**
- 后端：新增 `documents.py`（校验、存储配额、创建/替换/重新索引/删除、Worker 作业、URL 抓取、与知识库的钩子）、`file_store.py`、`api/knowledge_documents.py`、`schemas/knowledge.py`（文档部分）、迁移 `0031_knowledge_documents`（可降级）、`models/knowledge.py`（`KnowledgeDocument`）、`tests/test_knowledge_documents.py`（50 项：RAGFlow 与 Dify 各 25 项）；修改 `integrations/knowledge_engine.py`、`ragflow.py`、`dify.py`（统一的 `add_document / document_state / delete_documents / list_chunks`）、`knowledge.py`（`COPY_HOOKS` / `DELETE_HOOKS`）、`api/knowledge.py`、`config.py`（`file_storage_dir`）、`worker/*`、`compose.yaml`（共享卷 `knowledge_uploads`）。
- 前端：`pages/knowledge/DocumentsTab.tsx`、`KnowledgeDetailPage.tsx`、`i18n/*/knowledge.json`、`App.css`。
- 工具：`scripts/fake_ragflow.py`（文档接口）、`scripts/check_knowledge_e2e.py`（加入文档全流程）。

**行为**
- 上传：`POST /knowledge-bases/{id}/documents?filename=&on_conflict=ask|overwrite|keep_both`，**文件作为原始请求体**，一次一个（浏览器逐个上传；一次最多 20 个、单个 ≤ 50 MB 由界面与服务端共同限制，服务端边读边限，不会缓冲超过 50 MB）。校验：扩展名白名单（pdf / docx / html / htm / md / markdown / txt）+ 内容嗅探（PDF 魔数、DOCX 必须是含 `word/document.xml` 的 zip、文本不含 NUL 且能按 UTF-8 / GB18030 解码）+ 非空；文件名只取最后一段并去掉控制字符，存储路径只用 UUID，不含用户输入。
- 同名：默认 409 `knowledge.duplicate_name`，界面询问“覆盖 / 保留两者 / 跳过”；覆盖会删除旧文档（含引擎里的副本与原文件），保留两者给新文档加序号。
- 存储配额：`storage_gb` 按字节累计（计数器向上取整到 GB），硬限制超出返回 409 `plan.quota_exceeded`。
- 流水线（Worker）：`knowledge.upload_document`（读原文件 → 引擎 `add_document`，RAGFlow 上传 + 启动解析、Dify 创建即索引）→ `knowledge.poll_document`（指数退避 5s×1.5ⁿ，最长 120s，每次一个幂等键；结果回写分片数/失败原因）；所有作业可重复执行：已上传的不重传，轮询带引擎文档 ID，重新索引后旧轮询自动作废；数据集尚未创建时抛错重试；引擎未配置时文档置失败并提示“配置后点击重新索引”；`knowledge.sweep_documents` 每 5 分钟把处理超过 1 小时的文档置失败，防止永远“处理中”。
- URL：`POST …/documents/urls`（≤ 20 个）。每个地址先过公网 https 校验，任何一个不合格整批拒绝、不创建任何记录；Worker 抓取时逐跳重新校验并手动跟随重定向（≤ 3 次，跳到内网/http 被拒），大小 ≤ 20 MB，只接受 HTML / 文本 / PDF；抓取失败的文档带原因，可“重新索引”重试。
- 其他：列表（搜索、状态筛选、各状态计数）、详情、分片预览（直接问引擎）、重新索引、下载（`application/octet-stream` + `attachment` + `nosniff` + `CSP: sandbox`，上传的 HTML 不会在我们的域名下被渲染；URL 类型返回链接）、软删除（删原文件，入队删引擎副本）。写审计（`knowledge.document_*`）。
- 与知识库联动：文档数与状态（草稿 / 处理中 / 已索引）来自文档；有文档不能切到 FAQ 库；复制知识库会复制文档与原文件并重新索引；删除知识库会删除其原文件；改向量模型/切片策略时，重建会把所有文档指向新数据集并重新入队。

**验证**
- ruff；`test_knowledge_documents.py` 50 项 + `test_knowledge.py` 29 项通过（上传校验含伪造扩展名/内容、超大、路径穿越；同名三种处理；权限与跨租户/跨知识库；解析失败原因与重试；轮询幂等与过期轮询；退避与超时清扫；引擎未配置；删除与处理中删除；下载头；分片预览；URL 公网校验、重定向到内网被拒、大小上限、抓取失败重试；存储配额；重建重新索引；复制；删除知识库）。
- 真实栈端到端 `check_knowledge_e2e.py` 在真实 API + Worker + 假服务上，RAGFlow 与 Dify 两种配置都通过（含上传、索引、分片预览、下载、重新索引、拒绝伪造 PDF 与内网 URL、复制后重新索引）。

**遗留问题**
- **重建期间的检索缺口**：改向量模型/切片策略时，文档被重新入队并指向新数据集，切换发生在重新索引**开始时**而不是完成后，所以重建期间新数据集是逐步变满的（知识库状态显示“处理中”）。T-030 文档里“期间已挂载的助理继续用旧数据集”对**有文档的知识库**并不成立；完整做法是先在新数据集里索引完再切换（需要给文档增加“下一个数据集”字段并轮询到全部完成），T-033 接检索时再决定是否做。
- 不做的：KNW-041 的内容变更审批；URL 抓取的 DNS rebinding 窗口（T-071）；原文件目前存在 API 与 Worker 共享的本地卷，多机部署需换对象存储（只需替换 `file_store.py`）。
- 文档页面未做浏览器目视验收；上传在浏览器里是逐个文件顺序提交，没有进度条。

### Dify 真实上传实测记录（2026-11-05，Dify Cloud，经用户同意，另一个工作区的新 Key）

在该工作区创建临时知识库 `bw-verify-…`（Embedding 用工作区默认，即 `bge-m3` 的“用默认”分支），用 BeeWork 的 Dify 客户端依次上传同一份约 0.6 KB 的 Markdown，分别带三种切片策略的 `process_rule`；每份都轮询到结束后再删除文档，最后删除知识库。账号原有 7 个知识库未被触碰，结束时仍是 7 个，没有遗留。

已在真实 Dify 上确认：
- `POST /datasets/{id}/document/create-by-file`（`data` JSON 带 `indexing_technique`、`process_rule`、`doc_form=text_model`、`doc_language`，加 `file`）可用，返回 `document.id` 与 `batch`。
- `GET /datasets/{id}/documents/{batch}/indexing-status` 可用，状态走到 `completed`，`total_segments` 即分片数；**索引耗时 3.7–16.1 秒**（约 0.6 KB 的小文件，不代表 NFR-003 的 10 MB PDF）。
- 三种策略都被接受且效果不同：`automatic`（Semantic）得 1 个分片，`custom` 512 token（Fixed 512）得 24 个，`custom` 按标题分隔（By heading）得 12 个。分片内容符合预期（以“# 退货政策”开头）。
- `GET /datasets/{id}/documents/{doc}/segments` 可用，BeeWork 的分片预览能取到文本与总数。
- `DELETE /datasets/{id}/documents/{doc}` 可用，对已删文档再删，客户端按 404 视为成功。

新发现：
- **Dify Cloud 对知识库 API 有按订阅的请求频率限制**：连续密集轮询（约每 2 秒一次、总共几十次）后，删除知识库返回 `403 Sorry, you have reached the knowledge base request rate limit of your subscription.`，约 20 秒后恢复。BeeWork 的轮询是 5 秒起、每次 ×1.5、上限 120 秒，并且遇到该错误会作为引擎错误重试（作业重试带退避），所以不会丢数据，但大批量上传时可能被限流而变慢；需要时可再给轮询加全局节流。
- 没有验证的：PDF / DOCX / HTML 在 Dify 里的解析、`doc_form=qa_model`（FAQ，T-032）、检索（T-033）、10 MB 文件的耗时、上传失败时 `indexing_status` 的 `error` 取值。
- RAGFlow 的文档上传与解析仍未在真实实例上验证。

**审查修正**
- 修复“覆盖失败会弄丢旧文件”：覆盖、删除文档时原文件曾在事务提交**之前**就被删掉，若后面的步骤（比如存储配额检查）失败而回滚，文档记录恢复了但原文件已经没了。现在原文件在事务**提交之后**才删（回滚则不删）。已用测试复现并覆盖。
- 修复“重建中途失败留下半成品”：知识库重建时，某个重新索引钩子失败，已经改过的文档状态会被一并提交，导致文档指向还没切换的新数据集、并重复上传。现在失败时先回滚再标记失败，文档保持原状（已有测试）。
- 已知未处理：文档删除要求 `knowledge:edit` 而不是 `knowledge:delete`（PRD 没有区分文档删除权限，待确认）；抓取网页时 HTTP 客户端会读取环境里的代理设置；并发上传可能让存储略微超出配额；上传后若事务失败，磁盘上会留下一个无人引用的文件。
- 顺带修复一个 T-040 遗留的不稳定测试：`test_channels` 里“渠道首次发布走审批”这条用例自己起了一个后台 Worker 线程，又同时在测试线程里 `drain()` 并断言“至少处理了 1 个作业”，两者抢同一个作业，约三分之一的概率断言落空（与 T-031 无关）。现在改为等待渠道状态变为 active。

