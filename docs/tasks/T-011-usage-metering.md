# T-011 用量汇总：LiteLLM 花费 → 点数

**阶段**：2　**依赖**：T-002、T-010　**PRD**：RUL-020、RUL-022、SET-080、SET-081、SET-082、4.12 UsageRecord

## 设计

- 计量事实来自 LiteLLM：每次调用的 token 与花费按 team（= 租户 × 环境）记录在 LiteLLM 的 spend logs。BeeWork 不自己统计 token。
- Worker 每 5 分钟拉取增量（LiteLLM `/spend/logs` 或 `/global/spend/report` 等接口，按 `start_date` 增量 + 去重键），换算点数：`tokens_in/1000 × 模型输入扣点 + tokens_out/1000 × 模型输出扣点`（扣点取平台模型目录），写入 `usage_records`（日 × 租户 × 环境 × 模型 × 助理 × 来源）。请求 metadata 中携带的 `assistant_id`、`run_id`、`source`（live/debug/test/eval）由 T-025 在调用时传入 LiteLLM。
- **先确认接口**：实现前用本地 LiteLLM 实测 spend 相关接口的字段与分页，把结论写进本文件“实测记录”一节；若需要开启 LiteLLM 的 spend 记录配置，同步修改 `config/litellm/config.yaml`。
- 预算与告警（SET-082/083）：租户月度预算、阈值、接收人；跨阈值每月只通知一次（站内通知，依赖 T-023 时可后补）；开启“100% 暂停非生产流量”时，把开发/预发环境的虚拟 Key 置为 blocked，月初恢复。

## 界面

- “设置 → 订阅与用量”：本月已用点数 / 套餐点数、席位、预计月末用量（日均线性外推）、最近 6 个月柱状图 + 预算线、按助理拆分、导出 CSV（日 × 助理 × 模型 × 环境）。

## 验收标准

- [ ] 拉取任务幂等（重复拉取同一时间窗不重复计数），有测试（LiteLLM 响应用 MockTransport）。
- [ ] 本地用 LiteLLM 真实记录验证一次（无供应商 Key 时可在 LiteLLM 中注册一个 `mock_response` 的 deployment 产生 spend 记录），在 development-status 中记录结果。
