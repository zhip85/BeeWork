# T-032 FAQ 生命周期与推送

**阶段**：4　**依赖**：T-030　**PRD**：KNW-031、KNW-040、4.6 Faq、5.1 FAQ 状态机、6.1（运营可编辑 FAQ）

> **知识引擎（2026-11-09 决定）**：本任务及知识相关任务一律使用 **Dify**；RAGFlow 仅保留为可选引擎实现、不再验证。下文中的“RAGFlow”按“知识引擎（Dify）”理解，历史完成记录保持原样。

## 范围

1. `faqs` 表（BeeWork 为事实源）：问题、相似问法、答案、状态（草稿/已发布/已过期）、失效日期、命中数、`engine_chunk_id`（Dify 分段 ID）、`sync_status`。
2. 只有“已发布”的 FAQ 推送到该库的 FAQ dataset（`qa` 方法，相似问法作为可匹配的问题）；下线、过期、删除时从知识引擎移除。引擎为 Dify：FAQ 以问答模式（`doc_form: qa_model`）文档/分段推送，具体接口以 Dify Knowledge API 与 T-030/T-031 的实测结论为准。
3. 到期自动置为已过期（Worker 每日任务），修改失效日期后可重新发布。
4. 表单弹窗、列表（搜索、状态筛选、编辑、复制、删除）、CSV 批量导入（`问题,答案,相似问法以;分隔`，导入前预览条数）。
5. 运营角色可编辑 FAQ（6.1 中 FAQ 一行），其余知识权限仍只读。

## 验收标准

- [ ] 草稿 FAQ 不会被检索到（T-033 集成测试中验证）。
- [ ] 过期任务幂等。
- [ ] CSV 导入对格式错误逐行报告。

## 完成记录

**改动文件**
- 后端：新增 `models/faq.py`（`Faq`、`FaqHit`）、迁移 `0033_faqs.py`、`faqs.py`（状态机、推送/轮询/移除任务、到期任务、命中统计、CSV 解析、知识库复制/重建/删除钩子）、`schemas/faq.py`、`api/knowledge_faqs.py`、`tests/test_knowledge_faqs.py`（33 项）；修改 `integrations/{knowledge_engine,dify,ragflow}.py`（`add_faq`、检索结果带 `document_id`）、`runtime/{retrieval,compiler,executor}.py`（记录 FAQ 命中）、`worker/{runner,scheduler}.py`（每小时 `knowledge.expire_faqs`）、`tests/test_worker.py`、`scripts/fake_ragflow.py`（Dify `create-by-text`、`INDEX-FAIL` 失败注入）。
- 前端：新增 `pages/knowledge/FaqTab.tsx`（列表、搜索、状态筛选与计数、表单弹窗、复制、发布/取消发布、失败重试、删除、CSV 导入预览与逐行错误）；`KnowledgeDetailPage.tsx` 接入；中英文案 `faq.*`。

**迁移**：`0033_faqs`（`faqs`、`faq_hits`），可降级。

**接口**（均在 `/api/v1/knowledge-bases/{kb_id}/faqs` 下）：`GET` 列表（`q`、`status`、分页、各状态计数）、`POST` 新建、`GET/PUT/DELETE /{id}`、`POST /{id}/copy|publish|unpublish|retry`、`POST /import?dry_run=&publish=`（CSV 请求体 ≤1 MB、≤500 行）。权限：`faq:view` / `faq:edit` / `faq:publish` / `faq:delete`；运营能新建、编辑草稿与导入草稿，不能发布或删除。

**设计**
- BeeWork 是事实源；只有已发布且已同步（`sync_status=synced`）的 FAQ 才计入“已发布 FAQ”并被检索，草稿、已过期、推送中的都不会被检索到。
- Dify 的分段 API 是付费功能（真实环境 403），所以**一条 FAQ = 一个单分段文档**（`document/create-by-text`），文本为“问题 / 相似问法 / 答：答案”；编辑已发布的 FAQ 会用新文档替换旧文档。RAGFlow 的 `add_faq` 用 `qa` 方式（未验证）。
- 到期：失效日期当天仍有效，次日由每小时任务置为已过期并从引擎移除；任务幂等（已过期的不会再处理，移除只入队一次）。同一任务还会把卡住超过 30 分钟的推送标为失败，并清理 90 天前的命中记录。
- 命中数：运行成功后，把检索到的 FAQ 引擎文档映射回 FAQ，写入 `faq_hits`；列表显示近 30 天命中。

**验证**：ruff；迁移升降级 + `alembic check`；全量后端测试 507 项（含新增 33 项）通过；前端 `tsc -b`、lint、`i18n:check`、build 通过。验证覆盖：草稿不入引擎且检索不到、发布→推送→检索并以 `kind=faq` 引用作答、取消发布后检索不到、编辑替换文档、过期任务幂等、推送失败可重试、引擎故障自动重试、知识库复制/重建/删除、CSV 预览与逐行错误（表头、BOM、GBK、引号、超限）、权限（运营/查看者/401/跨租户 404）、审计。
**是否连过真实第三方引擎**：没有跑完整的真实端到端。Dify 的 `create-by-text` 单分段 FAQ 做过手工探测（可检索，得分 0.575–0.619）；其余用进程内假 Dify / 假 RAGFlow（真实 HTTP）验证。**没有在浏览器里目视检查 FAQ 页签。**

**遗留问题**
- 默认检索阈值 0.62 对 FAQ 的改写问法偏严（实测 0.575 会被过滤），建议 FAQ 单独设阈值或下调，需产品确认。
- 审批（KNW-041 / 策略事件“发布知识库更新”）没有接入 FAQ 发布。
- Dify 免费套餐有文档数与 API 速率限制，FAQ 多时会遇到；一条 FAQ 占一个文档。
- RAGFlow 的 `add_faq` 未验证。
- 命中数是新增的统计（`faq_hits`），只统计成功运行；测试控制台的运行也会计入。
