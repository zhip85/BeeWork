# T-084 调用追踪

**版本**：V0.3　**依赖**：T-054、T-025　**PRD**：3.8.5（OPS-040–042）、OPS-003、NFR-012、PER-011、8.3#13

## 第 0 步：实测（写入本文件“实测记录”）

确认 Langfuse Cloud v2 接口（`GET /api/public/v2/observations` 按 trace 过滤、分页、字段）与**删除 trace 的可用接口**（旧 `/api/public/traces/*` 已 410）。删除接口不可用时，保留期清理只删 BeeWork 侧记录，并在完成记录中写明 Langfuse 侧依赖其项目级数据保留设置。

## 范围

1. 列表（OPS-040）：数据来自 BeeWork 的 `runs` 表（不是 Langfuse），列 Trace ID、助理、延迟、Token、状态（成功 / 错误 / 慢，慢阈值默认 5 秒可配置）；筛选助理、渠道、状态、时间、延迟下限、来源（线上 / 调试 / 试运行 / 评测）；按 Trace ID、会话 ID 精确搜索；按租户与成员范围过滤。
2. 详情（OPS-041）：服务端按 run_id 从 Langfuse v2 读取 observations，组装瀑布图（阶段 span 名称由 T-054 固定）：每个 span 的开始、耗时、状态、输入/输出（已脱敏）、模型与 Token、点数、错误；可跳转会话。Langfuse 不可用时只显示 BeeWork 侧的 run 事件时间线并提示。
3. 保留期（OPS-042，8.3#13 暂按标准 7 / 专业 30 / 企业 90 天）：取套餐 `trace_retention_days` 与租户会话保留期的较小值，Worker 每日清理。
4. 平台管理员不能查看租户追踪内容（PER-011）。

## 验收标准

- [ ] 测试：跨租户 404；Langfuse 客户端用 MockTransport 的契约测试；保留期清理（可注入时钟）。
- [ ] 真实 Langfuse 中一次线上对话能在详情页看到完整瀑布（有 Key 时记录）。
