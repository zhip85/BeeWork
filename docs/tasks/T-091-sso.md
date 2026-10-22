# T-091 SSO：OIDC 与 SAML

**版本**：V1.0　**依赖**：T-010、T-093　**PRD**：3.10.4（SET-030–031）、SET-010（首次 SSO 登录自动创建）、5.1 成员状态机、PLT-081（SSO 权益）
**外部条件**：测试用 IdP（可用本地 Keycloak 容器验证 OIDC 与 SAML，内存允许时；或 Okta/Azure AD 开发者租户）

## 范围

1. 身份提供方（SET-030）：名称、协议（OIDC / SAML；LDAP 见 T-092）、域名、状态（启用 / 草稿 / 已停用）。
2. OIDC：Issuer（发现文档）、Client ID、Client Secret（加密）、Scopes、回调地址展示；授权码 + PKCE；校验 `iss`、`aud`、`nonce`、签名（JWKS 缓存与轮换）。
3. SAML：IdP Metadata URL（公网 https 校验）或上传 XML；展示 SP Entity ID 与 ACS URL；用成熟库（如 `python3-saml` / `pysaml2`，开工时选定并锁版本）；校验签名、Audience、时间窗、防重放（Assertion ID 缓存）。
4. 属性映射：邮箱、姓名、部门（映射到组织）、角色（映射到租户角色）。
5. 域名验证：DNS TXT 记录（`beework-verify=...`），验证通过前不可启用。
6. “测试登录”成功后才能启用（SET-031）。
7. 首次 SSO 登录自动创建成员（SET-010）：仅对已验证域名、且开启该选项；占用席位，席位不足时拒绝登录并提示。
8. 登录页：输入企业邮箱 → 按域名跳转对应 IdP。
9. 权益：SSO（专业版及以上）。全部配置变更与 SSO 登录写审计。

## 验收标准

- [ ] 安全测试：签名错误、过期断言、错误 audience、重放的 SAML 响应被拒；OIDC nonce 不符被拒。
- [ ] 用 Keycloak（或开发者 IdP）跑通 OIDC 与 SAML 各一次真实登录，记录在 development-status。
