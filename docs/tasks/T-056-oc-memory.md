# T-056 OC · 记忆

**版本**：V0.2　**依赖**：T-054、T-026　**PRD**：3.6.4（OC-020–022）、PLT-081 记忆权益、NFR-010

## 架构决策（开发负责人，可逆）

长期记忆的 Top-K 检索 **v1 用 PostgreSQL 全文检索 + 时间衰减排序**，封装在 `MemoryRetriever` 接口后；不引入向量库，也不把终端用户记忆写进 RAGFlow（知识库与个人记忆的隔离和删除要求不同）。若效果不足，改用 pgvector 需开发负责人另行批准。

## 范围

1. 短期记忆（OC-020）：历史窗口轮数 + 滑动窗口 token 上限（按模型 tokenizer 估算，超出按时间倒序截断）；替换 T-026 的固定条数窗口。
2. 长期记忆文件（OC-021）：`end_user_memories`（tenant、end_user、assistant（UserProfile 为空表示租户内共享）、file：profile/memory/tools/session、内容、类型 preference/history/fact、`expires_at`）。System Prompt 一行只做跳转。
3. 写入：会话结束（T-026 的 30 分钟判定或关闭）时入队 `memory.extract`，经 LiteLLM 抽取白名单类型写入 UserMemory、Profile（写入开关开时）、Session 摘要；写入前先经 PII 规则脱敏（T-055）；Tools 由运行时记录工具调用摘要。
4. 读取：按优先级注入，全量 / Top-K（OC-021），订阅开关控制 Profile 注入。
5. 文件配置弹窗（OC-022）：Top-K、写入/订阅开关、保留期 1–3650 天；用户级文件内容只读展示结构示例。
6. 保留期到期清理（Worker 每日）；按终端用户删除全部记忆的服务函数（供 T-080 用户详情与 T-086 数据删除调用）。
7. 权益：标准版只有短期；专业版短期 + 长期；企业版额外允许 UserProfile 跨助理共享（PLT-081）。
8. 编排总览记忆摘要。

## 验收标准

- [ ] 测试：token 截断边界；跨租户、跨终端用户记忆不可见；助理私有文件不跨助理；删除后不再注入。
- [ ] 测试：抽取任务幂等（同一会话重复入队不重复写）；写入内容已脱敏。

## T-056a 闭环切片先行范围（优先线）

**依赖**：T-054a、T-026。最小可跑通实现，其余仍属本任务：

- **做**：`end_user_memories` 表与迁移（含 `tenant_id`、`end_user_id`、`file`、类型、`expires_at`，助理私有文件按 `assistant_id` 隔离）；`MemoryRetriever` 接口 + PostgreSQL 全文检索 + 时间衰减实现；`memory` 阶段读取并注入 UserProfile、UserMemory、Session 摘要；会话结束（T-026 的判定）入队幂等的 `memory.extract`，经 LiteLLM 抽取后写入；写入前用 T-026 的 `beework/pii.py` 基础脱敏；保留期到期清理；“按终端用户删除全部记忆”服务函数。
- **不做（留给 T-056 剩余部分）**：短期记忆 token 滑动窗口、套餐权益分级（暂不区分，按开启即可用）、文件配置弹窗与页面、编排总览摘要、可配置 PII 规则（T-055）。
- 测试：跨租户、跨终端用户不可见；助理私有文件不跨助理；删除后不再注入；抽取幂等；写入内容已脱敏。
- 隐私约束：记忆属终端用户个人数据，本切片必须保证按租户与终端用户隔离、可整体删除；不得写入 RAGFlow。

### T-056a 完成记录

**改动文件**：新增 `models/memory.py`、`memory.py`、`memory_jobs.py`、`runtime/memory_stage.py`、迁移 `0020_memory.py`（可降级）、`tests/test_memory.py`（18 项）；修改 `runtime/{executor,compiler}.py`（记忆提供者、线程安全）、`worker/{runner,scheduler}.py`、`models/__init__.py`、`models/conversations.py`、`tests/{test_runtime,test_worker}.py`、`scripts/check_runtime_e2e.py`（记忆抽取/读回校验，并清理自己产生的会话数据）。

**验证**：`ruff`、`./scripts/test-postgres.sh`（迁移升降级 + `alembic check` + 153 项）、`check_gateway_e2e.py` 通过。真实栈 `check_runtime_e2e.py`（`mock_response` 模型）：经 LiteLLM 抽取得到 `{memory:1, profile:1, session:1}`、手机号已脱敏、同一终端用户读回三类记忆；会话结束钩子在真实 Worker 里也触发了 `memory.extract` 任务。单元测试覆盖：相关度/时间衰减/中文匹配/Top-K/过期、按用户/租户/助理隔离（含跨租户）、OC 开关、白名单类型、脱敏、幂等与替换、跳过条件、非 JSON 触发重试、钩子与任务、保留期清理、按用户删除与审计、运行时注入与跨用户隔离。仍是 mock 模型 + 检索桩。

**遗留问题 / 需开发负责人确认**
- **与规格的偏差**：规格决定 v1 用“PostgreSQL 全文检索 + 时间衰减”。PostgreSQL 内置全文检索（`simple`/`english`）无法给中文分词，所以这里改为：PostgreSQL 先按租户/用户/文件取最近 200 条候选，再在应用内用关键词 + 中文双字相关度 × 时间衰减排序。接口仍是 `MemoryRetriever`，未引入向量库，也没写入 RAGFlow。若要严格用 PG 全文检索，需要 `zhparser`/`pg_trgm` 之类的扩展，请确认。
- 未做（留给 T-056 剩余部分）：短期窗口的 Token 滑动截断（目前用 DSL `history_messages` 固定条数）、套餐分级（标准版只有短期、企业版跨助理画像）、文件配置弹窗与页面、编排总览摘要；Tools 文件（按用户历史工具调用）未实现，OC 中只是占位配置。
- 抽取只能读取已入库（已按租户策略脱敏）的消息；若租户关闭了日志脱敏，抽取结果仍会再脱敏一次后写入。
- 没有会话“被关闭”的入口，目前只有 30 分钟无消息会触发抽取。
- 抽取提示词与字段（user_memory / profile / session_summary）是本任务定的，质量未用真实模型评估。
- 记忆目前只写在 `console`（调试）会话的终端用户上；真实渠道用户的身份与会话来自 T-041 以后。
- `memory.retention` 的审计没有租户归属（全局任务）。
