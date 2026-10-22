# T-066 多智能体协作组

**版本**：V0.2　**依赖**：T-061　**PRD**：AGT-032、3.3 协作组说明、PLT-081 多智能体权益、8.3#2

## 范围

1. 协作组 = `agents.kind='agent' AND is_team=true`，卡片元信息「多智能体协作」；编辑器顶部“协作模式”（Router / Plan & execute / Group chat / Sequential / Parallel）与“主控智能体”（从已连接成员中选）；成员至少 2 个（配置检查）。
2. 运行时按模式编译为 LangGraph 图：Router（主控选一个成员处理）、Sequential（按成员顺序传递）、Parallel（并发调用后由主控汇总）、Plan & execute（主控产出计划逐步分派）、Group chat（主控主持多轮发言，最大轮数默认 6）。深度、超时规则同 T-061。
3. 权益：**开发负责人暂定映射，待产品确认**——专业版：Router（委派）、Sequential（转交）、Parallel（并行）；企业版：全部模式。映射写在套餐权益 JSON（`agent_team_modes`），可由平台调整，不硬编码。
4. 8.3#2：本期不单独建“协作组”页签，沿用 Agent 页签 + 元信息。

## 验收标准

- [ ] 每种模式的单元测试（假模型）与超时/深度测试。
- [ ] 测试：套餐未开通的模式不可保存为可用（403 `plan.feature_locked`）。
