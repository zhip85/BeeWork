# T-051 统一行业字典与平台行业术语表

**版本**：V0.2　**依赖**：T-001　**PRD**：3.16.1（PLT-120–126）、8.3#6、3.12.3 第 1 步行业、3.10.1 所属行业

## 产品决策（开发负责人，按 PRD 建议执行）

8.3#6 行业枚举不统一：建立**平台统一行业字典** `industries`（code、中英文名、排序、状态），开通向导、企业信息、行业术语表、模板市场全部引用它。初始值取各处并集并做映射：保险 insurance、银行金融 banking、跨境电商/零售电商 retail_ecommerce、医疗 healthcare、通用办公 general_office、公共部门 public_sector。迁移把租户现有行业值映射到 code；无法映射的置为 `general_office` 并在迁移说明列出。

## 范围

1. `industries` 字典与平台维护接口（新增、改名、停用；有引用时不可删除）。
2. `industry_glossaries`（行业术语库：名称、描述，关联一个行业 code）与 `platform_terms`（标准术语、别名数组、定义）；同一术语库内标准术语唯一（不区分大小写）。
3. 平台后台“行业术语表”页（PLT-120–125）：左侧术语库列表、右侧术语表与搜索（匹配术语与别名）、新增/编辑/删除术语、新建/重命名/删除术语库（有租户继承时提示 N 个租户）。
4. 导入（PLT-124）：CSV/TXT 文件或粘贴；解析规则按 PRD（表头识别、中英文逗号与 Tab、别名 `;` 转逗号、第三列之后并入定义）；导入前预览条数，重复术语可选覆盖/跳过。解析器做成纯函数，T-053 租户导入复用。
5. 预置数据（PLT-126）：零售电商、保险、银行金融三库。
6. 所有写操作写平台审计。

## 验收标准

- [ ] 导入解析器单元测试覆盖 PRD 列出的全部格式规则与错误行报告。
- [ ] 迁移升降级通过，旧行业值映射有测试。

## 完成记录

**改动文件**
- 后端：新增 `models/glossary.py`（`Industry`、`IndustryGlossary`、`PlatformTerm`）、迁移 `0035_industries.py`（三张表 + `tenants.industry` + 种子）、`glossary_parser.py`（**纯函数**解析器，T-053 复用）、`glossary.py`（校验、导入、使用计数）、`schemas/glossary.py`、`api/platform_glossary.py`、`tests/test_glossary.py`（21 项）；修改 `models/__init__.py`（`Tenant.industry`、审计文案）、`governance.py`（行业范围改为读 `tenants.industry`，金融 = banking / insurance）、`schemas/__init__.py` 与 `api/platform_tenants.py`（创建 / 编辑 / 列表带行业）、`api/__init__.py`。
- 前端：平台后台“行业术语表”页（`pages/platform/glossary/`：术语库列表、术语表与搜索、术语与术语库弹窗、导入弹窗、行业字典管理）、租户创建 / 编辑表单增加“所属行业”、`i18n/*/glossary.json`、侧栏入口。

**迁移**：`0035_industries`（可降级）。写入 6 个行业（insurance、banking、retail_ecommerce、healthcare、general_office、public_sector）和 PLT-126 的三个预置术语库（零售电商、保险、银行金融，各 8 条术语）。**`tenants` 表原来没有行业列，所以没有旧值可映射**：新增列 `industry`，现有租户全部为 `general_office`（迁移说明）；规格里“旧行业值映射有测试”改为测试种子数据与代码映射的覆盖。`industry` 不加外键，由接口按字典校验（停用的行业不能再被指派）。

**接口**（平台管理员）：`/platform/industries`（列表带引用数、新增、改名 / 停用、删除——被租户或术语库引用时 409）；`/platform/glossaries`（列表、新建、改名 / 改行业、删除——有租户继承时需 `confirm=true`，409 说明租户数）；`/platform/glossaries/{id}/terms`（列表带搜索、新增、改、删）与 `…/terms/import`（`dry_run` 预览、`on_duplicate=skip|overwrite`，UTF-8 / GBK，≤1 MB）。全部写操作写平台审计。

**验证**：ruff；迁移升降级 + `alembic check`；全量后端 568 项（新增 21 项）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build；无头 Chrome 看了页面（真实种子数据）。验收标准：导入解析器单测覆盖 PRD 的全部规则（表头识别、三种分隔符、别名 `;`、第三列之后并入定义、BOM / 换行、空行、逐行错误含重复术语、条数上限）；迁移升降级通过。**未在浏览器里完整点过导入弹窗与行业字典管理。**

**遗留问题**
- “N 个租户继承中”暂按“同行业租户数”计算（`inheriting_tenant_count`），T-053 落地租户继承选择后改为实际继承数。
- 行业只在平台租户表单里可选；企业信息页的“所属行业”（SET，T-090）与开通向导（T-100）、模板市场仍待对接同一字典。
- 术语还没有被运行时使用（术语表 / 问题改写阶段在 T-057）。
- 行业名称的英文名目前只存储，界面显示中文名。
