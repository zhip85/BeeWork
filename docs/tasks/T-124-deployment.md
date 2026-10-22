# T-124 部署：Helm、单机与私有化开关

**版本**：V1.0（生产门禁）　**依赖**：T-120、T-121、T-123　**PRD**：NFR-018、PLT-020 子域名、3.14.1 白标与自定义域名、8.3#15（MCP stdio 私有化）

## 范围

1. Helm chart：api、worker、web、迁移 Job、Ingress（`*.beework.ai` 泛域名 + 每租户子域名路由、自定义域名证书）、HPA、PodDisruptionBudget、资源限制、Secret 引用（不在 values 中放明文）。第三方引擎按各自官方 chart 部署，本 chart 只配置地址与密钥。
2. 单机部署：`compose.prod.yaml`（与开发 compose 分开）、TLS（反向代理）、初始化脚本（创建平台管理员、平台默认数据）。
3. 私有化开关（NFR-018）：关闭多租户（单租户模式：隐藏平台后台的租户运营与计费，开通一个固定租户）、关闭计费、开启 MCP stdio（T-072 预留）、放开 LDAP/工具的内网地址策略（显式配置的网段白名单）。
4. 白标：自定义域名（加购）绑定、证书签发流程说明。
5. 部署文档 `docs/ops/deploy.md`。

## 验收标准

- [ ] 在 kind 或 k3d 集群上 `helm install` 后冒烟测试通过；单机 compose.prod 冒烟测试通过。
- [ ] 单租户模式下平台计费与租户列表接口返回 404。
