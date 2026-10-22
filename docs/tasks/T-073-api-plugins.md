# T-073 API 插件

**版本**：V0.2　**依赖**：T-072　**PRD**：3.5.5（CAP-040–042）、3.5.2

## 范围

1. `api_plugins`：类型 REST / GraphQL、认证方式（None / API key（Header 名 + 值）/ Bearer / OAuth 2.0 客户端凭证（Token URL、Client ID、Client Secret、Scope））、Base URL（公网 https 校验）、spec 原文、解析出的工具清单、已选工具、凭证密文与掩码。
2. 解析（CAP-040）：OpenAPI 3.0/3.1（YAML/JSON，粘贴或上传）每个 path + method 一个工具，名称取 operationId（缺失时 method + path 规范化），参数取 parameters + requestBody 生成 JSON Schema；GraphQL SDL 每个 Query/Mutation 字段一个工具。解析失败返回行号与原因；`$ref` 只允许文档内引用（拒绝外部 URL 引用，防 SSRF）。
3. 工具勾选与测试调用（CAP-041）：选择工具、填写参数、查看响应（经 T-071 执行，写操作在测试时也需二次确认）。
4. 状态：无 spec 保存为草稿；有 spec、测试调用成功且至少选 1 个工具为可用。审批与权益同 T-072。
5. OAuth 2.0 令牌由执行器在调用时获取并缓存（加密，按过期时间刷新）。

## 验收标准

- [ ] 解析器单元测试：Petstore 示例、缺 operationId、外部 `$ref` 被拒、GraphQL Mutation 标记为写操作、错误行号。
- [ ] 测试：非 Base URL 主机的请求被执行器拒绝（同主机约束）。
