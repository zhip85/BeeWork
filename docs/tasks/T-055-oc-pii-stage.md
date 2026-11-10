# T-055 OC · PII 与安全基线执行

**版本**：V0.2　**依赖**：T-052、T-054　**PRD**：3.6.3（OC-010–013）、PLT-141、PLT-144、SET-070 日志脱敏、NFR-009
**外部条件**：分类器（classifier）类规则需要 Presidio；未部署时这类规则显示“检测器未配置”

## 范围

1. 助手层 PII 规则（OC-010–011）：`draft_config.oc.pii`（两个开关 + 助手规则/覆盖列表），复用 T-052 的引擎与规则表组件（助手模式）；继承链三级展示。
2. 检测器注册表 `beework/governance/detectors.py`：
   - `regex`、`keyword`：BeeWork 内实现；正则执行加超时/长度上限，防灾难性回溯。
   - `classifier`：Presidio Analyzer REST 适配器（`compose.yaml` 中作为可选 profile `pii`，默认不启动；本机内存不足时不启用）。
   - `llm_judge`：经 LiteLLM 调用平台配置的“安全判定模型”（平台设置项），超时 1.5 秒。
   - **失败策略（开发负责人决定，可逆）**：`classifier`/`llm_judge` 检测器不可用或超时时放行并在 trace 标记“检测器失败”、计数告警；不因外部检测器故障阻断全部流量。
3. 运行时（OC-013、PLT-144）：输入阶段在记忆组装前执行，输出阶段对模型回复和工具返回执行；多条命中按“拦截 > 脱敏 > 标记”；脱敏保留首尾字符；拦截时不调用模型、返回拦截提示（未配置用平台默认）；提示词攻击、内容安全基线与 PII 规则同时执行。
4. 命中记录：规则、层级、动作、阶段写入 run 事件与 Langfuse span，**不记录原始敏感值**。
5. 替换 T-026 的 `beework/pii.py`：会话日志脱敏改用该租户“生效的输出 + 输入脱敏规则”。
6. 编排总览 PII 摘要（3.6.2）。

## 验收标准

- [ ] 单元测试：动作优先级、脱敏格式、拦截时模型未被调用（假模型调用计数为 0）、强制规则在 PII 检测关闭时仍生效、正则超时保护。
- [ ] 测试：trace/事件/日志中搜不到测试用的身份证号原文。
- [ ] NFR-001：100 条规则时 PII 阶段 P95 < 50 ms（不含外部检测器），测试中记录。

## 完成记录

**改动文件**
- 后端：新增 `governance/detectors.py`（检测器注册表：`regex`、`keyword`、`classifier`〔Presidio REST〕、`llm_judge`〔经 LiteLLM〕）、`governance/pii_engine.py`（扫描引擎：动作优先级、脱敏格式、命中记录、日志脱敏）、`governance/runtime_policy.py`（把平台 / 租户 / 助手三层 PII 规则与下发给租户的安全基线合成一次运行的规则集；助手层视图与总览计数）、`runtime/pii_stage.py`（`pii_input`、`pii_output` 两个阶段）、`api/assistant_oc_pii.py`（助手层规则预览接口）、`tests/test_pii_engine.py`（20 项）、`tests/test_pii_runtime.py`（17 项）；修改 `runtime/{oc,compiler,memory_stage,executor,tracing}.py`、`pii.py`、`conversations.py`、`assistant_oc.py`（PII 摘要）、`config.py`、`api/__init__.py`、`requirements.txt`（显式依赖 `regex`）、`tests/test_assistant_oc.py`、`compose.yaml` 与 `.env.example`（可选 `pii` profile 的 Presidio、`PRESIDIO_ANALYZER_URL`、`SAFETY_JUDGE_MODEL`）。
- 前端：OC 页“PII 规则”阶段 `pages/orchestration/PiiStagePanel.tsx`（助手模式，复用 T-052 的规则表与弹窗；继承链三级、两个开关、编辑继承规则生成覆盖、恢复、删除、新增），`i18n` 文案。
- 迁移：无（助手层规则存在 `draft_config.oc.pii`，随版本快照冻结）。

**运行时行为（OC-013、PLT-144）**
- 输入阶段在记忆之前执行：命中“拦截”→ 立即返回拦截提示（规则里的提示，未配置用平台默认），**不检索、不调用模型**；命中“脱敏”→ 之后所有阶段（记忆、检索、模型）只看到脱敏后的消息；多条命中按 拦截 > 脱敏 > 标记。输出阶段对模型回复执行：只要有可能改写回复的输出规则，回复在生成期间不流式下发，规则执行完后一次性发出（避免未脱敏文本被流出去）；没有输出规则时仍逐 token 流式。
- 规则集：平台与租户规则每次运行开始时读取（修改下一条消息即生效），助手层来自版本快照；强制规则在“PII 检测”关闭时仍生效；安全基线里启用且范围匹配的提示词攻击 / 内容安全行与 PII 规则同时执行；强制的 PII 基线行会把它引用的平台规则强制打开；草稿与不匹配范围的基线不执行；数据保留类不在运行时。
- 脱敏格式：保留首尾字符、中间用 `*`——≥ 11 位保留前 3 后 4（身份证 `110***********4518`、手机号 `138****5678`），8–10 位保留 2 + 2，更短保留 1 + 1，长度不变。
- 命中记录：规则、层级、动作、阶段、命中次数写入阶段 step 事件、`done` 事件与 `runs.output.pii`，**从不记录匹配到的文本**；检测器失败（未配置 / 超时 / 出错）单独记在 `failures`。
- 日志脱敏（SET-070）：`conversations.store_text`、运行内容清理改用该租户生效的“脱敏 / 拦截”规则（仅进程内的正则与关键词检测器）；租户没有任何脱敏规则时回退到原来的固定模式；Langfuse trace 用同一套规则脱敏。

**检测器与失败策略（可逆决定）**：正则用 `regex` 库，每个模式每个窗口 50 ms 超时、模式 ≤ 1000 字符，长文本按 2 万字窗口扫描（不漏检）；`classifier` 调 Presidio `POST /analyze`（超时 1 s，规则的匹配内容填实体类型）；`llm_judge` 经 LiteLLM 调 `SAFETY_JUDGE_MODEL`（超时 1.5 s，命中则整段文本命中，脱敏动作会把整段替换为占位提示）。**检测器不可用 / 超时 / 出错时放行并记录“检测器失败”，不阻断流量。**

**验证**：ruff；全量后端 677 项（含 T-053；本任务新增 40 余项）；前端检查；无头 Chrome 看了 OC“PII 规则”阶段页与编排总览。验收标准：动作优先级、脱敏格式、拦截时模型 / 检索调用次数为 0、强制规则在检测关闭时仍生效、正则超时保护（`(a|aa)+$` 被 50 ms 切断而不是卡住）——均有测试；事件 / 运行记录 / 消息 / trace 中搜不到测试用的身份证号与手机号原文——已测；NFR-001：100 条规则（60 正则 + 40 关键词）、约 700 字消息、300 次，**p50 0.45 ms、p95 0.51 ms**（不含外部检测器）。
**是否连过真实第三方引擎**：**没有**。Presidio 与 LLM 判定只用假 HTTP 服务验证；本机未启动 Presidio（`docker compose --profile pii up -d presidio-analyzer` 可选）。实栈跑了 `scripts/check_runtime_e2e.py`：十个阶段、事件顺序与流式 token 通过（开发库里有输出规则，所以 token 合并成一条），最后一项“done 事件检索模式为 stub”是 T-033 之后就过期的旧断言，与本任务无关。**没有在浏览器里点过助手层规则的编辑、恢复、保存草稿流程。**

**遗留问题**
- 工具返回（T-071）的输出护栏尚未接入；知识片段（参考资料）不做 PII 扫描。
- 开发库里的两条分类器示例规则（姓名、家庭住址）与两条大模型判定示例规则在检测器未配置时会被记为“检测器失败”并放行；要让它们生效需要启动 Presidio / 配置 `SAFETY_JUDGE_MODEL`。
- 助手层覆盖尚未注册进平台规则删除钩子（T-052 遗留）：平台删除一条规则后，助手快照里指向它的覆盖会休眠（不执行），不会报错。
- 记忆写入仍用固定模式脱敏（`memory.py`），未接生效规则。
- `scripts/check_runtime_e2e.py` 的 stub 检索断言需要更新（非本任务引入）。
