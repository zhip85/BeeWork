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
