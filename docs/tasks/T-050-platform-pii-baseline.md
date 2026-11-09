# T-050 平台 PII 规则与安全基线

**版本**：V0.2（OC 的前置）　**依赖**：T-001、T-010　**PRD**：3.16.2（PLT-130–133）、3.16.3（PLT-140–144）、PLT-071、4.8、8.3#4、#5、#20

## 目标

建立 PII 规则三级继承的“平台层”和安全基线，使租户层（T-052）、助手层（T-055）和运行时有可继承的上级。

## 产品决策（开发负责人，按 PRD 建议执行）

- 8.3#5：PII 单条规则**只在“PII 规则”页维护**；安全基线中分类为 PII 的条目是“规则集引用”（指向平台 PII 规则的集合），不再重复存储匹配内容（PLT-143）。
- 8.3#20：平台允许临时停用强制规则（PLT-131），但**停用、新增、下线强制规则都走双人复核**（PLT-071）。
- 8.3#4：本期租户只维护 PII 类规则；提示词攻击、内容安全、数据保留类只由平台基线下发。

## 数据模型（`models/governance.py`）

- `platform_pii_rules`：名称、匹配方式（regex/keyword/classifier/llm_judge）、匹配内容、检测阶段（both/input/output）、命中动作（mask/block/flag）、拦截提示、`enforced`、`enabled`、`row_version`、审计字段。
- `security_baseline_rules`：PLT-140 字段（分类 pii/prompt_attack/content_safety/data_retention、阶段含 data、执行级别 enforced/recommended、下发范围 all/enterprise/region_cn/industry_finance、状态 enabled/draft、动作、匹配方式、匹配内容、拦截提示）；PII 分类行引用规则集。
- 迁移写入 OC-013 默认 5 条平台 PII 规则与 PLT-140 预置 7 条基线（PII 类两条改为规则集引用）。
- 校验：正则必须可编译（5.7），关键词按逗号/换行切分。

## 服务

- `baseline_rules_for_tenant(db, tenant)`：按套餐、区域、行业（T-051 行业字典）匹配下发范围（PLT-141），供 OC 总览计数和运行时使用。
- `retention_limit_for_tenant(db, tenant)`：数据保留类规则对租户“会话数据保留期”的上限（强制）或默认值（推荐）（PLT-142）；T-026 的保留期校验改为调用它。

## 接口与界面（平台后台）

- “PII 规则”页（PLT-130–132）：表格、启用开关、强制开关、编辑/删除；删除平台规则时下级覆盖自动转为本层自建（PLT-133，T-052 落地后生效，先留服务接口）。
- “安全基线”页（PLT-140 通用列表）：状态条、搜索、新建/编辑/删除。
- 写操作写平台审计；强制规则的新增、停用、下线生成待复核操作（复用现有双人复核机制）。

## 验收标准

- [ ] 测试：强制规则变更未经第二人复核不生效；发起人不能复核；72 小时失效。
- [ ] 测试：下发范围匹配（企业版 / 中国区域 / 金融行业）正确；草稿不下发。
- [ ] 非法正则保存返回 422 并指出原因。

## 完成记录

**改动文件**
- 后端：新增 `models/governance.py`（`PlatformPiiRule`、`SecurityBaselineRule`、`PlatformChangeRequest`）、迁移 `0034_platform_governance.py`（含 OC-013 的 5 条平台 PII 规则与 PLT-140 的 7 条基线）、`governance.py`（校验、强制规则变更识别与双人复核、`baseline_rules_for_tenant`、`retention_limit_for_tenant`、PII 规则集解析、行业解析与删除钩子）、`schemas/governance.py`、`api/platform_governance.py`、`tests/test_platform_governance.py`（15 项）；修改 `compliance.py`（保留期上限与默认值接入基线）、`config.py`（`PLATFORM_RULE_REVIEW`，默认 `true`）、`models/__init__.py`（注册与审计文案）、`api/__init__.py`。
- 前端：平台后台新增“PII 规则”“安全基线”两页与共用的“待复核变更”面板（`pages/platform/governance/`）、`api.ts` 增 `apiRequestWithStatus`、`i18n/*/governance.json`、侧栏入口。

**迁移**：`0034_platform_governance`（三张表，可降级；升级时写入 5 条平台规则与 7 条基线）。

**接口**（平台管理员）：`GET|POST /platform/pii-rules`、`PUT|DELETE /platform/pii-rules/{id}`；`GET|POST /platform/baseline-rules`、`PUT|DELETE …/{id}`；`GET /platform/rule-changes`、`POST /platform/rule-changes/{id}/approve|reject`。新增 / 修改返回 201 / 200，高危变更返回 202 与待复核记录。

**设计**
- PII 单条规则只在“平台 PII 规则”维护；基线里的 PII 类是**规则集引用**（`ruleset`：全部 / 仅强制 / 指定规则），不存匹配内容（PLT-143）。删除平台规则时，基线规则集自动移除对它的引用；下级覆盖转本层的钩子 `PLATFORM_RULE_DELETE_HOOKS` 留给 T-052。
- 高危变更（PLT-071）：新增强制规则、规则变为强制或取消强制、停用强制规则、删除强制规则（基线同理，“强制”= 执行级别为强制，“停用”= 启用→草稿）走双人复核，72 小时失效，发起人不能复核，驳回必填原因，发起后规则被改动则复核时 409。修改强制规则的内容、启用已停用的强制规则直接生效。同一规则同时只能有一个待复核变更。
- 下发（PLT-141）：`baseline_rules_for_tenant` 只取“启用”的基线，按套餐（企业版）、区域（`cn_shanghai`）、行业匹配；草稿不下发。保留期（PLT-142）：`retention_limit_for_tenant` 给出强制规则的上限与推荐规则的默认值，租户“数据与隐私”页的可选保留期与默认值已接入（T-026 的校验经 `compliance.max_retention_days`）。
- 全部写操作写平台审计（`platform_pii_rule.*`、`baseline_rule.*`、`platform_rule.change_*`）。

**验证**：ruff；迁移升降级 + `alembic check`；全量后端 547 项（新增 15 项）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；用无头 Chrome 看了两个页面与两个弹窗（真实迁移数据）。验收标准逐条：强制规则变更未经第二人复核不生效、发起人不能复核、72 小时失效——已测；下发范围（企业版 / 中国区域 / 金融行业）与草稿不下发——已测；非法正则 422 并指出原因——已测。**未连任何第三方引擎；未在浏览器里走完“提交 → 他人复核”的完整点击流程。**

**遗留问题**
- 租户“行业”字段还不存在（T-051），行业范围的下发通过 `INDUSTRY_RESOLVERS` 钩子实现并用测试桩验证；T-051 落地前没有租户属于金融行业。
- 平台规则目前只被存储与暴露为服务接口，**运行时尚未执行**（T-055 做 OC · PII 执行）；基线里提示词攻击 / 内容安全类规则的匹配同样未执行（分类器、大模型判定类也依赖后续任务）。
- 种子数据中除 PLT-140 明确的几条外，其余基线（身份证与银行卡检测、角色扮演越狱检测、违法内容过滤）的阶段、级别、范围、状态是开发时的假设，需产品确认。
- 复核人在“PII 规则 / 安全基线”页内处理待复核变更，没有并入“操作审计”页的待复核列表。
- 强制规则的双人复核默认开启；开发环境只有两个平台账号（`platform@` 与 `reviewer@`），可用 `PLATFORM_RULE_REVIEW=false` 关闭。
