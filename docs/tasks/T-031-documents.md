# T-031 文档上传、解析状态同步、删除与重建

**阶段**：4　**依赖**：T-030　**PRD**：KNW-030、4.6 Document、NFR-003、NFR-009、NFR-020

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
