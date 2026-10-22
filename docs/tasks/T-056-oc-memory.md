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
