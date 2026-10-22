# T-063 Chatflow 会话设置、中断追问与调试预览

**版本**：V0.2　**依赖**：T-062、T-026　**PRD**：3.3.6、3.3.7、OC-064 第 5 条、OPS-010（Chatflow 兜底转人工）

## 范围

1. 会话设置（3.3.6）：开场白、推荐问题（≤ 4）、记忆范围（thread / 按用户跨会话 / 不保留）、携带历史轮数 1–50、每轮检查点、信息不足时追问与最多追问轮数、兜底回复、转人工开关；默认值按 PRD。
2. 检查点：使用 LangGraph 官方 `langgraph-checkpoint-postgres`，表放在独立 schema `langgraph`（由库自身 `setup()` 创建；Alembic `include_object` 排除该 schema，保证 `alembic check` 干净）；thread_id = 会话 ID（或按用户范围的派生 ID）。
3. 中断追问：模型判断信息不足时用 LangGraph `interrupt` 暂停，run 状态 `waiting_input`；下一条消息从中断节点恢复；超过最多追问轮数进入兜底。OC 路由对“处于中断等待的 Chatflow”优先交给它（OC-064 第 5 条，由 T-058 调用本任务提供的查询函数）。
4. 兜底：返回兜底回复；开启转人工时调用转人工钩子（T-081 实现，未完成时记录事件并在看板备注）。
5. 调试预览（3.3.7）：对话窗、新会话、推荐问题、每条回复下的节点轨迹、interrupt 提示、会话状态面板（状态、thread_id、轮数、messages 数、记忆、checkpoint、节点路径）；使用草稿配置真实执行，`source=debug`，扣点不计入运营统计。

## 验收标准

- [ ] 测试（假模型）：中断 → 恢复从中断节点继续；追问上限后兜底；关闭检查点时不写 checkpoint。
- [ ] `alembic check` 在 checkpoint 表存在时仍通过（PostgreSQL 测试）。
