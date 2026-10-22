# T-092 LDAP 登录与 SCIM 用户同步

**版本**：V1.0　**依赖**：T-091　**PRD**：SET-030–031（LDAP）、3.14.1 SSO / SCIM 权益（企业版 SCIM）
**外部条件**：测试用 LDAP（如 OpenLDAP 容器）；SCIM 用 Okta/Azure AD 的 SCIM 测试工具或 SCIM 合规测试脚本

## 范围

1. LDAP（SET-031）：Server URL（ldaps 优先，`ldap://` 需显式确认）、Bind DN、密码（加密）、Base DN、用户过滤器（参数化，防 LDAP 注入）；“测试登录”；登录时绑定校验并按属性映射同步姓名/部门。LDAP 服务器地址同样做内网地址策略检查（私有化部署可放开，T-124 开关）。
2. SCIM 2.0（企业版）：`/scim/v2/Users`、`/Groups` 的 CRUD 与过滤（`userName eq`），Bearer 令牌（租户级、只存哈希）；Users 映射成员（active=false → 暂停并吊销会话与个人 API Key），Groups 映射角色或组织（配置项选择）。席位限制同样生效。
3. 权益：SCIM 仅企业版；写审计（来源标注 SCIM）。

## 验收标准

- [ ] SCIM 合规基本用例（创建、查询、PATCH active、删除）测试通过。
- [ ] LDAP 注入测试（过滤器中的特殊字符被转义）。
