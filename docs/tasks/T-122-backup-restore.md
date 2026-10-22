# T-122 备份与恢复

**版本**：V1.0（生产门禁）　**依赖**：T-121、T-124　**PRD**：NFR-013、technical-plan 第 7 节

## 范围

1. PostgreSQL：基础备份 + WAL 归档（选 pgBackRest 或 WAL-G，开工时确定并锁版本），RPO ≤ 15 分钟；备份保留 30 天；备份加密并存放于对象存储独立前缀/桶。
2. 恢复：时间点恢复脚本与 Runbook，RTO ≤ 4 小时；季度演练流程与记录模板。
3. 第三方引擎：RAGFlow、Langfuse 为云服务，不在 BeeWork 备份范围；提供“从 BeeWork 业务数据重建”的任务（重新创建 RAGFlow dataset 并重新上传文档、重新同步 LiteLLM team/key/deployment），作为灾难恢复步骤。
4. 一次完整演练：在空环境中恢复到指定时间点，跑一遍冒烟测试，记录实际 RPO/RTO 到 development-status。

## 验收标准

- [ ] 演练记录与 Runbook 提交到 `docs/ops/`；重建任务有测试。
