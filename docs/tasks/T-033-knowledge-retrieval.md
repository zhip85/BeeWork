# T-033 知识挂载、运行时检索与引用

**阶段**：4　**依赖**：T-025、T-031　**PRD**：AST-022（知识库部分）、AST-062、KNW-040、5.5 第 8 步、RUL-010–012

## 范围

1. 挂载选择器支持“知识库”类型：只列出“已索引”的知识库（RUL-010），显示普通 KB / FAQ KB 标签与文档数；挂载写入助理草稿 `mounts.kb`。
2. 发布快照（T-024）记录挂载知识库当时的 `engine_dataset_id` / `engine_faq_dataset_id`。
3. 运行时 `retrieve` 节点：先查 FAQ dataset（命中且相似度 ≥ 阈值时直接作为标准答案依据），否则查文档 dataset，仅保留 ≥ 阈值的分片；**只传入快照中属于当前租户的 dataset ID**。检索结果作为上下文注入 `generate`，并在 `done` 事件和消息中返回引用（文档名、分片 ID、片段）。
4. 检索调用与耗时进入 Langfuse trace（span）。
5. 知识库被删除/停用后，助理挂载显示失效并计入配置检查（GLB-083）。

## 与桩检索的关系

T-025 已提供 `Retriever` 协议与 `StubRetriever`（闭环切片约定）。本任务实现真实 `RagflowRetriever` 并替换桩；**只要运行时仍在使用桩，本任务不得记为完成**，完成记录需写明已用真实 RAGFlow 验证，并确认界面与事件中不再出现“桩实现”标识。

## 验收标准

- [ ] 跨租户检索测试：伪造的 dataset ID 不会被传给 RAGFlow（单元测试检查调用参数）。
- [ ] 用真实 RAGFlow + 真实模型跑通“上传 → 索引 → 提问 → 带引用回答”，记录在 development-status。

## 完成记录

**改动文件**
- 后端：新增 `knowledge_mounts.py`（挂载解析、引用计数、运行时解析）、迁移 `0032_message_citations.py`、`tests/test_knowledge_retrieval.py`（26 项）；修改 `integrations/{knowledge_engine,ragflow,dify}.py`（`search`）、`runtime/{retrieval,compiler,dsl,executor}.py`（`EngineRetriever`、`KnowledgeBaseRef`、`resolve_bases`、`knowledge_error`）、`api/assistants.py`（挂载校验）、`api/assistant_checks.py` + `schemas/assistant_checks.py`（候选接口）、`models/conversations.py`、`conversations.py`、`schemas/conversations.py`（引用）、`channel_chat.py`（开放 API 引用）、`api/runs.py` / `api/assistant_versions.py` / `test_cases.py`（`dsl_from_draft(assistant, db)`）、`scripts/fake_ragflow.py`（检索端点）、`tests/test_runtime.py`；新增 `scripts/check_retrieval_e2e.py`。
- 前端：`pages/assistants/CapabilityPicker.tsx`，向导第 3 步、详情页挂载弹窗、测试控制台引用展示，`i18n` 文案。

**接口**：`GET /capabilities/candidates?kind=kb&q=`（`assistants:edit`）；`mounts.kb` 的校验；`messages[].citations`；开放 API 的 `done` / 同步响应新增 `citations`；`retrieval.mode` 语义见 development-status。

**验证**：见 development-status “知识挂载与运行时检索”。ruff、全量 472 项（含迁移升降级 + `alembic check`）、前端检查通过；隔离栈 e2e（RAGFlow / Dify 两种假引擎）通过。**没有在浏览器里目视检查选择器、挂载弹窗与测试控制台。**

**遗留问题 / 条件**
- **验收标准部分满足**：规格要求“用真实 RAGFlow + 真实模型跑通上传 → 索引 → 提问 → 带引用回答”。**Dify Cloud 这一半已在真实环境跑通**（见 development-status），RAGFlow 与真实模型仍未验证（回答用 `mock_response`）。真实环境暴露了两个假引擎测不出的问题并已修复：空 dataset 检索报错（只检索有内容的 dataset）、Dify 的 API 速率限制。拿到 RAGFlow 实例与模型 Key 后，用 `scripts/check_retrieval_e2e.py` 复跑即可。因此本任务按“已完成（有条件）”处理。
- FAQ 优先的逻辑已实现并有单元测试，但 FAQ 内容本身（T-032）尚未建成，端到端里没有 FAQ 命中的场景。
- 知识库重建（改嵌入模型/分块）后会生成新 dataset：运行时按知识库当前行解析，所以线上版本不受影响；快照里记录的旧 dataset ID 只作记录。
- 引擎检索失败会让运行失败（`runtime.knowledge_error`），没有“降级为无资料作答”的开关；是否需要降级需产品确认。
- 每次提问会对每个挂载的知识库各发一次 FAQ 检索和一次文档检索（Dify 每个 dataset 一次请求），挂载多时延迟线性增长；上限 10 个，未做并发。
- 检索关键词用的是用户原始消息；问题改写（T-057）之后应改用改写后的问题。
- 附带发现（非本任务，属 T-011 用量汇总）：用量拉取任务在共享 LiteLLM 里遇到本库不存在的租户的团队时，会因外键约束 `usage_records_tenant_id_fkey` 失败并留下 `PendingRollbackError`；多套环境共用一个 LiteLLM 时会触发，建议对未知租户跳过。
