# T-074 Skill：上传、安全检查与运行时加载

**版本**：V0.2　**依赖**：T-072、T-002　**PRD**：3.5.3（CAP-020–027）、CAP-007、CAP-009、5.1 自建 Skill 状态机、NFR-009

## 架构决策（开发负责人，可逆）

本期 **Skill 中的脚本不执行**（NFR-009 要求沙箱，沙箱方案未定）。Skill 只提供指令与静态资源；安全检查发现脚本时给出“含脚本，本期不执行”的提示，不因此判为失败。脚本执行能力另立任务，需开发负责人批准沙箱方案。

## 范围

1. 上传（CAP-020–022）：.zip / .md / .skill，≤ 20 MB；zip 安全解压（拒绝路径穿越、符号链接、压缩炸弹：解压后总大小与文件数上限）；必须有带 name 与 description frontmatter 的根目录 SKILL.md；自动填充名称与描述。包存储在租户隔离路径（本地卷，T-121 后换对象存储）。
2. 自动化安全检查（CAP-023–025）：Worker 任务依次执行 4 项（包结构与清单、危险脚本与可执行文件、密钥与敏感信息（正则 + 熵检测）、权限与外部依赖），每项状态写库并推送进度，前端显示 N/4；失败项列出文件路径与原因；更换文件或未通过时重新检查。
3. 状态门槛（CAP-026）：安全状态不是 passed 时为草稿、不可挂载、不可启用。
4. 平台内置 Skill（CAP-009）：平台维护（转人工、情绪识别、多语言回复），租户只读，可停用/启用、复制（复制后为自建、需重新检查）。平台侧维护页面放在平台后台“平台定义”下的简单列表。内置“转人工”被调用时触发 T-081 的转人工钩子。
5. 运行时（CAP-027）：模型先只看到 name + description；需要时通过内置工具 `load_skill(name)` 加载 SKILL.md 与文本资源（经 T-071 执行器记录）。
6. 启用受“启用第三方 Skill / MCP”审批策略约束；权益同 T-072。

## 验收标准

- [ ] 安全测试：路径穿越 zip、压缩炸弹、缺 frontmatter、含 AWS 密钥样例的包分别被正确拒绝或标记失败。
- [ ] 测试：未通过检查的 Skill 不出现在挂载候选中；内置 Skill 不可删除。

## 完成记录

**改动文件**
- 后端：`models/capabilities.py` 增加 `PlatformSkill`、`Skill` + 迁移 `0044_skills`（可降级）、`skill_package.py`（安全解压与四项检查）、`skill_service.py`（内置 Skill 同步、包存储、检查任务、挂载解析器与候选、`SkillProvider`）、`api/capabilities.py`（Skill 上传 / 改名 / 换包 / 重新检查、内置 Skill 只读保护、平台内置 Skill 接口）、`schemas/capabilities.py`、`worker/runner.py` 与 `runtime/executor.py` 导入以注册任务与 provider；`capabilities.py` 把 `MOUNT_KINDS` 改为函数内导入（修复冷启动时的循环导入，见下）。
- 前端：`pages/capabilities/SkillFormPage.tsx`（拖入 / 选择上传、名称与描述自动填充、N/4 检查进度轮询、失败项文件与原因、重新检查、内置只读说明、包内文件清单），能力页开放 Skill 页签；平台后台“平台定义 → 内置 Skill”（`PlatformSkillsPage`：编辑名称、描述、SKILL.md、启停）；`capabilities.json` / `platform.json` 中英文。
- 接口：`POST /capabilities/skills?filename=&name=&description=`（原始请求体，≤ 20 MB，流式计数）、`PUT /capabilities/skills/{id}`、`POST /capabilities/skills/{id}/package`、`POST /capabilities/skills/{id}/recheck`；通用的 enable / disable / copy / delete / references 沿用 T-072；`GET /platform/skills`、`PUT /platform/skills/{id}`（平台管理员）。

**规则落实**
- 上传（CAP-020–022）：.zip / .skill / .md，≤ 20 MB；内存中解压，拒绝路径穿越、绝对路径、符号链接、单文件膨胀超过 100 倍或声明大小不符（压缩炸弹）、解压后总量 > 50 MB、文件数 > 300；SKILL.md 必须在根目录或唯一顶层目录下，且有 name 与 description frontmatter；名称 / 描述为空时取自 frontmatter。包按 `skills/{租户}/{能力}/v{版本}` 存放。
- 四项检查（CAP-023–025，Worker 逐项执行并逐项提交）：包结构与清单（隐藏文件、字段长度）；危险脚本与可执行文件（二进制 / ELF / PE / Mach-O 失败，脚本仅提示“含脚本，本期不执行”）；密钥与敏感信息（AWS、私钥、GitHub、Slack、`sk-` 等模式 + 高熵赋值，列出文件与行号）；权限与外部依赖（内网 / 本机 / 云元数据地址失败，依赖清单、明文 http 与外部域名列为提示）。更换文件后重新检查，旧版本的检查任务不会覆盖新状态。
- 状态门槛（CAP-026）：安全状态不是 passed 时为草稿、不进候选、不能启用（提示“此 Skill 需要先通过安全检查才能启用。”）；检查通过后以上传者身份提交“启用第三方 Skill / MCP”审批，无法提交（如没有其他审批人）时把原因记在 Skill 上显示。
- 内置 Skill（CAP-009）：平台维护三个（转人工、情绪识别、多语言回复），各租户首次打开列表时自动出现且可用；租户可停用 / 启用（不走审批）、复制（复制为自建包并重新检查）；不能改、不能换包、不能删除；平台改名或停用立即作用于所有租户。
- 运行时（CAP-027）：每个 Skill 一个加载工具 `skill:{能力ID}`，模型先只看到名称与描述；调用时返回 SKILL.md（可选 `resource` 读取包内文本资源，脚本源码加“不会执行”说明，二进制拒绝），经 T-071 执行器记录；内置“转人工”加载时触发 `LOAD_HOOKS["handoff"]`（T-081 注册）。
- 权益：`skills`（标准版锁定，403 `plan.feature_locked`）。

**验证**：ruff；`tests/test_skills.py` 31 项（包解析与拒绝、四项检查、上传与检查流转、失败与门槛、超大上传 413、重名、换包与旧任务、改名、重新检查、审批与“没有其他审批人”、内置的出现 / 只读 / 停用 / 复制 / 平台修改与停用、按需加载与资源、内置钩子、不可用不可加载、挂载与引用、套餐锁定、权限与跨租户、按租户存储）；与能力相关的 6 个模块重跑；全量 `./scripts/test-postgres.sh` 891 项通过（含迁移升降级；第一轮有 6 项失败，原因是能力目录 / 门户应用测试注册假 Skill 后直接删除、连带删掉了真实注册，已改为恢复原注册）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 上传 SKILL.md、看到 0/4 检查面板、检查通过后列表显示“安全检查通过 · SKILL.md”和三个内置 Skill。
**是否连过真实第三方引擎**：不涉及。

**遗留问题**
- **开发环境 Worker 主循环被卡住（与本任务无关，需另行修复）**：`usage.sync` 拉到一个已不存在租户（`5080bde5…`）的 LiteLLM 花费记录，插入 `usage_records` 触发外键错误后会话进入失败状态，主循环每轮都失败，**所有后台任务都不再执行**。浏览器验证时，Skill 检查是我在 api 容器里手动执行的。建议 T-011 跟进：跳过未知租户的花费记录，并在 `execute_job` 中对单个任务的数据库错误先回滚再标记失败，不影响其他任务。
- 本任务发现并修复了一个冷启动循环导入：`beework.capabilities` 在模块顶部导入 `schemas.assistants`，而 `schemas/__init__` 会导入 API 包，单独 `import beework.capability_service`（T-072）或 `beework.skill_service` 会失败；应用和 Worker 因为导入顺序碰巧不受影响。
- Skill 中的脚本不执行（按规格），执行能力需另立任务并批准沙箱方案；“检查进度推送”用前端轮询实现，没有走运行事件推送。
- 运行时还没有调用 `skill_tool_specs` / `invoke_tool`（T-061）。

## 需求变更（2026-11-12）

增加“步骤型”Skill（声明式步骤与依赖）与执行器，说明型 Skill 行为不变，见 `T-164`。
