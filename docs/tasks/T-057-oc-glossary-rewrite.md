# T-057 OC · 助手术语表与问题改写

**版本**：V0.2　**依赖**：T-053、T-054　**PRD**：3.6.5（OC-030–037）、3.6.6（OC-040–051）、NFR-001

## 范围

1. 助手术语表（OC-030–036）：`draft_config.oc.glossary`（继承开关 + 本层术语/覆盖/隐藏），复用 T-053 的引擎与组件（助手模式）。
2. 运行时术语（OC-037）：生效术语构成词典；别名扩展为确定性步骤（不调用模型）；检索时作为同义词传给检索查询；命中术语的定义注入提示词。
3. 问题改写配置（OC-040–048）：模型只能选当前环境已启用的对话模型；各开关、阈值、轮数、语言、个性化提示词（≤ 2000 字）。
4. 改写执行：一次 LiteLLM 调用、结构化 JSON 输出各步骤结果（指代消解、口语规范化、拆解、翻译），别名扩展在本地完成；1.5 秒超时或失败时用原文（OC-051）。原文保存在对话中，改写结果只用于路由与检索，两者都写入 trace。
5. “仅意图置信度低时改写”的执行顺序在 T-058 中实现（先用原文做语义路由，置信度不足再改写重路由）；本任务提供 `rewrite(query, context, config)` 纯服务。
6. 改写测试（OC-050）：`POST /assistants/{id}/oc/rewrite-test` 按步骤返回变化，只展示有变化的步骤；调用记为 `source=debug`。
7. 编排总览术语与改写摘要。

## 验收标准

- [x] 单元测试（假模型）：超时回退原文；别名扩展；关闭改写时原样进入路由。
- [x] 改写测试接口返回 PRD 示例「这个 SKU 咋退货？」的各步骤结构（真实模型时记录在 development-status）。


## 完成记录

**改动文件**
- 后端：新增 `governance/glossary_runtime.py`（助手层术语视图与继承链计数、运行时字典、别名扩展、提示词“术语”段）、`runtime/rewrite.py`（纯函数 `rewrite(query, history, config, llm)`：一次结构化 JSON 调用，指代消解 → 口语规范化 → 拆分 → 翻译；超时 1.5 s / 出错 / 输出不可解析均回退原问题）、`runtime/glossary_stage.py`（`glossary_rewrite` 阶段）、`api/assistant_oc_glossary.py`（`POST /assistants/{id}/oc/glossary/preview`、`POST /assistants/{id}/oc/rewrite-test`）、`tests/test_rewrite.py`（30 项）；修改 `runtime/{oc,compiler,executor,llm}.py`（配置、状态键、检索词 / 提示词、改写模型走 LiteLLM）、`api/assistant_oc.py`（保存时校验改写模型必须是本租户已启用的对话模型，422 `rewrite.model_unavailable`）、`assistant_oc.py`（术语表 / 问题改写摘要）、`api/__init__.py`、`tests/test_assistant_oc.py`。
- 前端：OC 页“术语表”（`GlossaryStagePanel.tsx`：继承链三级、继承租户开关、助手级新增 / 编辑继承项生成覆盖 / 恢复 / 删除 / 全部恢复）、“问题改写”（`RewriteStagePanel.tsx`：开关、模型、置信度阈值、四种策略与参数、自定义提示词、改写测试框），`stagePanels.ts` 注册，zh-CN / en 文案，样式追加在 `design-system.css`。
- 迁移：无（助手层配置存在 `draft_config.oc.glossary` / `oc.rewrite`，随版本快照冻结）。

**运行时行为**
- 术语（OC-037）：每次运行开始时按 行业 → 租户 → 助手 合成生效术语（修改下一条消息即生效）；问题里出现标准术语或别名（拉丁词按整词、中文按子串、不分大小写）时，把其余名称作为同义词追加到**检索词**，并把命中术语的定义写进系统提示词“术语”段。**别名扩展完全在本地完成，不调模型**，与改写开关无关。
- 改写（OC-040–051）：关闭时原问题原样继续；只改写检索 / 路由用的问题，**对话里保留用户原文**，改写结果、步骤、同义词写入阶段 step 事件、span 元数据与 `runs.output.query`。失败（`timeout` / `error` / `invalid`）一律用原问题，运行照常回答。
- 改写测试：只返回发生变化的步骤，顺序 指代 → 规范化 → 别名扩展 → 拆分 → 翻译；调用记为 `source=debug` 并占用租户点数配额；需要 `orchestration:edit`。

**验证**：ruff；全量后端测试通过（含新增 30 项与更新的 `test_assistant_oc`）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看了“术语表”“问题改写”阶段页。验收标准：超时回退原文（0.2 s 超时 0.5 s 内返回）、别名扩展、关闭改写时原样进入检索——均有假模型测试；改写测试接口对 PRD 示例「这个 SKU 咋退货？」返回 `normalize`、`alias_expansion` 两步结构（假模型）。
**是否连过真实第三方引擎**：**没有**。改写只用假模型验证，**未取得真实模型对「这个 SKU 咋退货？」的改写结果**（本环境无模型密钥）；改写测试的网关调用路径（`build_rewrite_model`）未经真实 LiteLLM 验证。

**可逆决定与与 PRD 的差异**
- 改写默认**关闭**（PRD 默认开）：每次改写多一次模型调用与最多 1.5 s 延迟，由租户显式开启。
- “仅在意图置信度低时改写”属于路由（T-058）：本任务提供纯函数 `rewrite()` 与开关 / 阈值配置，阶段内只在“关闭该开关（每条都改写）”时执行改写；开关保持默认开时由 T-058 决定。
- 拆分得到的子问题目前合并为一条检索词（“；”连接），按子问题分别检索留给 T-058。

**遗留问题**
- 未在浏览器里点过助手术语的新增 / 编辑 / 恢复 / 删除与保存草稿流程；开发库里没有行业术语数据，截图中术语为空。
- `scripts/check_runtime_e2e.py` 的 stub 检索断言过期（非本任务引入）。
- 改写阶段不对知识片段或工具返回做处理。
