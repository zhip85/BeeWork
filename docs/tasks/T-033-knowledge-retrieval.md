# T-033 知识挂载、运行时检索与引用

**阶段**：4　**依赖**：T-025、T-031　**PRD**：AST-022（知识库部分）、AST-062、KNW-040、5.5 第 8 步、RUL-010–012

## 范围

1. 挂载选择器支持“知识库”类型：只列出“已索引”的知识库（RUL-010），显示普通 KB / FAQ KB 标签与文档数；挂载写入助理草稿 `mounts.kb`。
2. 发布快照（T-024）记录挂载知识库当时的 `ragflow_dataset_id` / `ragflow_faq_dataset_id`。
3. 运行时 `retrieve` 节点：先查 FAQ dataset（命中且相似度 ≥ 阈值时直接作为标准答案依据），否则查文档 dataset，仅保留 ≥ 阈值的分片；**只传入快照中属于当前租户的 dataset ID**。检索结果作为上下文注入 `generate`，并在 `done` 事件和消息中返回引用（文档名、分片 ID、片段）。
4. 检索调用与耗时进入 Langfuse trace（span）。
5. 知识库被删除/停用后，助理挂载显示失效并计入配置检查（GLB-083）。

## 与桩检索的关系

T-025 已提供 `Retriever` 协议与 `StubRetriever`（闭环切片约定）。本任务实现真实 `RagflowRetriever` 并替换桩；**只要运行时仍在使用桩，本任务不得记为完成**，完成记录需写明已用真实 RAGFlow 验证，并确认界面与事件中不再出现“桩实现”标识。

## 验收标准

- [ ] 跨租户检索测试：伪造的 dataset ID 不会被传给 RAGFlow（单元测试检查调用参数）。
- [ ] 用真实 RAGFlow + 真实模型跑通“上传 → 索引 → 提问 → 带引用回答”，记录在 development-status。
