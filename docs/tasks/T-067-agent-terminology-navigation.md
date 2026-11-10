# T-067 术语统一与导航改版（助理 → 智能体）

**版本**：V0.2　**依赖**：T-003　**PRD**：演进 PRD 2、4.1–4.3、18.1（`docs/PRD-ai-portal-and-agent-evolution.md`，下称“演进 PRD”）；原型 `docs/ai-portal-prototype.html`

## 背景

演进 PRD 取消“助理 / Assistant”这个独立的用户概念：用户看到的只有“智能体 / Agent”。原来的助理（提示词、模型、挂载、OC、渠道、版本、测试、会话）就是现在的“智能体”；Chatflow / Workflow 移到“流程画布”（T-060 返工）。本任务只改**用户可见**的术语与导航，不改后端表、接口路径和权限码。

## 范围

1. **文案**：中英文字典里所有面向用户的“助理 / 助手 / Assistant”改为“智能体 / Agent”（创建智能体、搜索智能体、智能体名称、未命名智能体、智能体规则……）。审计分类“助手”改为“智能体”。后端返回给用户的中文错误信息里的“助理”一并改（只改文案，不改错误码）。
2. **左侧导航**按原型分组：
   - 工作台
   - 构建：智能体（原 `/assistants`）、知识、流程画布（`/flow-studio`，T-060 返工后接入）、能力
   - 发布与运营：AI 门户（T-140 起接入）、渠道、运营、审批
   - 管理：设置、审计日志、成员与权限（保持现状）
   未落地的入口按现有 `SectionPlaceholder` 规则显示，不能伪装成可用。T-060 第一版在导航里加的“智能体”（指向 `/agents`）删除。
3. **路由兼容**：`/assistants`、`/assistants/:id/...` 等现有路径继续可用（深链不失效）。新增 `/agents` → `/assistants` 的别名重定向**不做**：`/agents` 在 T-060 第一版里被 Chatflow / Workflow 占用过，返工后统一用 `/flow-studio`。
4. **权限与角色说明**：角色管理页的模块名“助理”改为“智能体”，原“智能体”（Chatflow / Workflow 的 `agents:*`）改为“流程画布”。权限码本身不变（`assistants:*` 仍指智能体，`agents:*` 指流程），在 `rbac.py` 注释和 architecture.md 写明这一对应关系。
5. 不改：数据库表名、API 路径、权限码、审计动作名（`assistant.*`）、i18n key 名。

## 验收标准

- [ ] `grep` 中英文字典与前端组件：用户可见文案中不再出现“助理 / 助手 / Assistant”（代码标识、key 名除外），`npm run i18n:check` 通过。
- [ ] 导航分组与原型一致；旧深链 `/assistants/:id?tab=...`、`/assistants/:id/oc/:stage` 仍可打开。
- [ ] 后端测试中断言中文错误文案的地方同步更新，全量测试通过。

## 不在范围

- 智能体创建方式：**已确认（2026-11-10）保持“简化新建弹窗 + 详情页就地编辑”**，不恢复原型的 4 步向导；本任务只改弹窗与详情页里的术语。

## 完成记录

**改动文件**
- 前端：`i18n/{zh-CN,en}/*.json`（全部用户可见值“助理 / 助手 / Assistant”→“智能体 / Agent”；`knowledge.json` 的“助理与智能体引用”改为“智能体与流程引用”）、`Dashboard.tsx`、`AuditLogPage.tsx`、`RoleManagementPanel.tsx` 的写死文案、`App.tsx` 导航（分组：工作台 / 构建：智能体、知识库、流程画布、能力目录 / 发布与运营：渠道、审批中心 / 管理）、`nav.json` 增加 `group.*`、`flowStudio`（原“编排中心”占位入口改为“流程画布”，路径 `/flow-studio`，权限 `agents:view`）、`App.css` 分组标题样式。
- 后端：27 个 `.py` 文件里的用户可见中文（错误信息、审计动作描述、审计分类、权限模块名、网页挂件报错、开放 API 说明）；`rbac.py` 中 `assistants` 模块名为“智能体”、`agents` 模块名为“流程画布”；`static/widget.js` 报错文案；测试里 5 处断言文案同步。
- 文档：`architecture.md` 新增“术语与内部标识对应”一节。

**验证**：ruff；`./scripts/test-postgres.sh` 全量 709 项通过（含迁移升降级）；前端 `tsc -b`、lint、`i18n:check`、`style:check`、build 通过；无头 Chrome 看过智能体列表与详情页、新导航分组。
**是否连过真实第三方引擎**：不涉及。

**未改动（有意）**：表名、接口路径、权限码、审计动作名、i18n key；发给大模型的提示词（`runtime/rewrite.py`、`memory.py`、`test_cases.py` 评测提示词）；历史迁移文件；`scripts/` 里的 e2e 测试数据。

**遗留问题**
- 已写入审计表的旧记录描述保持“创建助理”等原文；已有智能体里保存的问候语等用户数据不改。
- 管理端“AI 门户”导航入口留给 T-140；`/portal` 路径留给员工端 T-142。
- 开发库里 `entitlements` 等已落库的套餐描述仍是旧文案（迁移不可改，新建套餐用新文案）。
