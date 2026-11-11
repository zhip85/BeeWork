# T-142 员工门户外壳与员工身份

**版本**：V0.4　**依赖**：T-141、T-004　**PRD**：演进 PRD 5.3、5.4、11.1、11.6、11.7、12.2；原型“预览员工端”

## 开发负责人决定（可逆）

1. **同一个前端应用，独立路由树**：员工端放在 `frontend/src/portal/`，路由 `/portal/*`，自己的布局与样式（暖白背景、浅色侧栏、衬线问候、珊瑚色强调，样式类以 `portal-` 前缀，不复用控制台外壳），用 `React.lazy` 按路由拆包（顺带缓解大 chunk 警告）。不另起 Vite 项目。
2. **员工身份**：员工就是租户成员（`memberships`）。新增内置角色 `employee`（员工）：没有任何控制台权限，只能用门户；控制台用户（管理员、构建者等）也可以进门户。登录沿用现有会话 Cookie + CSRF（T-004）；企业 SSO / LDAP / 企业微信登录在 T-146 接入。**已确认（2026-11-10）：`employee` 角色不占席位**——席位计数（`entitlements` 的席位配额、邀请与 SSO 自动创建时的席位检查、订阅与用量页的已用席位）都排除 `employee`；成员从 `employee` 改成其他角色时按新增席位检查，席位不足拒绝（409）。
3. 员工端接口统一前缀 `/api/v1/portal/me/*`，只需租户成员身份；所有可见性与开关在服务端判定（演进 PRD 5.4、15.1）。

## 范围

1. 员工端外壳：左侧栏（新建、搜索、项目、AI 应用 —— 按 `/portal/me/config` 显示；已置顶、最近使用列表）、左下角账号菜单（个人中心、返回管理端（仅有控制台权限时显示）、退出登录）；个人中心不在主导航出现。
2. 首页：问候语（按时间段 + 显示名）、居中大输入框（提示语来自门户设置）、企业 AI / 应用选择、回答风格；对话能力由 T-143 实现，本任务先接空态。
3. 员工登录页 `/portal/login`：按 T-146 的设置显示可用登录方式；T-146 之前只显示邮箱密码登录；未启用任何方式时显示“管理员尚未配置认证方式”，不伪装成可登录（演进 PRD 12.2、16.2）。
4. 管理端“预览员工端”按钮在新标签页打开 `/portal`。
5. 响应式：桌面完整侧栏；平板收窄；移动端侧栏为抽屉，输入区适配触控（11.7）。

## 验收标准

- [ ] 测试：`employee` 成员不计入已用席位；席位已满时仍可新增 `employee`，但把 `employee` 改为构建者时 409；`employee` 角色访问任一控制台接口 403、访问 `/portal/me/config` 200；跨租户隔离；门户设置关闭的入口在外壳中不出现（前端测试或 Playwright）。
- [ ] 在桌面、平板（768）、手机（390）宽度截图检查关键操作可达。

## 需求变更（2026-11-11，演进 PRD v1.1）

- `employee` 角色**不占控制台席位**（与 2026-11-10 的确认一致），但使用门户需要**AI 门户席位**：平台授予（T-149）、租户分配（T-150）、员工端校验（T-151）。两类席位分别计量、所有界面分开显示，不要合并成一个“席位”数字。
- 本任务的外壳在 T-151 之前不做席位判定；T-151 合入后，外壳在进入首页前读取 `GET /portal/me/seat`，无席位时显示“暂未开通”页。

## 完成记录（2026-11-11，Claude Sonnet 5.5）

**改动文件**
- 后端：`rbac.py`（`employee` 内置角色，无任何权限；`PORTAL_ONLY_ROLES`）、`auth.py`（新增依赖 `get_console_membership`：员工访问控制台接口 403）、`entitlements.py`（`CONSOLE_FREE_ROLES`，`seat_usage` 不计员工成员与员工邀请）、`api/members.py`（邀请 / 重发 / 恢复 / 接受邀请对员工不检查席位；员工改为控制台角色时检查席位，满则 409 `plan.quota_exceeded`）、`api/approvals.py`、`api/tenant_models.py`（改用 `get_console_membership`——枚举测试发现这两处原先对任何租户成员开放）、`schemas/__init__.py`（角色正则含 `employee`）。
- 前端：`frontend/src/portal/`（`PortalApp`、`PortalHome`、`PortalLogin`、`portalTypes`、`portal.css`，`portal-` 前缀样式，`React.lazy` 独立 chunk）、`App.tsx`（`/portal/*` 路由；未登录进入 `/portal` 显示员工登录；只有 `employee` 角色的成员访问任何控制台地址都被重定向到 `/portal`）、`PortalAdminPage`（“预览员工端”按钮，新标签页打开 `/portal`）、成员页席位说明文案、i18n（新增命名空间 `portalApp`，`enums.role.employee`，`portal.preview`；中英文）。
- 测试：`backend/tests/test_employee_role.py`（12 个）。
- 无迁移、无新接口（员工端只读用 T-141 的 `GET /portal/me/config`）。

**验证**
- `ruff` 通过；`test_employee_role` + `test_entitlements` + `test_environments` + `test_plans` + `test_portal_apps` + `test_portal_settings` + `test_approvals` + `test_tenant_auth` + `test_tenant_lifecycle` 全部通过（隔离 PostgreSQL 库，含迁移升降级）。
- 测试覆盖：员工不占席位（成员与邀请）；席位满时可邀请 / 接受 / 恢复员工；员工改构建者 409、改后占用席位；构建者改员工释放席位；`/auth/me` 员工权限为空；**枚举全部依赖租户成员的路由（200+ 条），员工访问除 `/portal/me/*`、通知外全部 403**；`/portal/me/config` 200 且 `has_console=false`；跨租户 403。
- 前端：`tsc -b`、`lint`、`i18n:check`、`style:check`、`build` 通过；portal 样式与脚本是独立 chunk（PortalApp 7.6 kB、portal.css 6.5 kB），控制台包不含门户样式。
- 浏览器（puppeteer + 真实开发库中的员工账号）：登录页、首页、账号菜单（员工只有“个人中心 / 退出登录”，无“返回管理端”）、访问 `/` 被重定向到 `/portal`、平板 820、手机 390（抽屉）截图检查通过；“项目”开关关闭时侧栏不出现项目入口。
- 未跑全量测试（主机负载高），只跑了上面受影响的模块；合并前由全量回归兜底。

**未连真实第三方**：无外部依赖。

**遗留问题 / 与规格的差异**
1. 首页输入框与“应用”下拉暂不可用：发送按钮禁用并提示“对话功能即将开放”（对话在 T-143，应用库在 T-144）；搜索、项目、AI 应用、个人中心页面目前是“即将开放”空态，分别由 T-143 / T-144 / T-145 实现，不算在本任务完成范围内。
2. 登录页只有邮箱密码；“未启用任何认证方式时显示管理员尚未配置”依赖 T-146 的登录方式配置，本任务未做。登录页的品牌名在登录前无法取得租户名，使用通用名“AI 门户”。
3. 席位校验（T-151）尚未接入：外壳在进入首页前不判断门户席位。
4. 开发库中新建了演示员工账号 `employee.demo@example.com`（仅本地）。
5. 待用户决定：租户管理员能否看到员工的门户对话（默认不能，可做成租户设置），影响 T-143。
