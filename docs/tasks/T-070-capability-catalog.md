# T-070 能力目录、挂载选择器与引用完整性

**版本**：V0.2　**依赖**：T-033、T-060　**PRD**：3.5.6（CAP-050–054）、AST-022、AST-062–066、AST-070、GLB-083、5.4（RUL-010–013）、KNW-042

## 目标

把“哪些资源可挂载、谁引用了谁、哪些引用已失效”做成统一服务，避免每个模块各算一套。

## 设计

- 资源类型注册表：kb、agent、chatflow、flow、mcp、api、skill，每类提供 `list_available(db, tenant_id, q)`、`status_of(obj)`（CAP-051 状态映射）、`display_meta(obj)`。
- 引用表 `resource_references`：`tenant_id`、引用方（类型 + ID + 配置层：draft / live）、被引用方（类型 + ID）、关系（mount / route_target / node / connected_agent / driver / fallback）。由各模块在保存草稿、设为可用、发布版本时**整体重算并替换**该引用方的行（同一事务），不做增量。
- 服务：`references_to(target)`（删除/停用前提示数量与列表，RUL-012、KNW-042）、`invalid_references(referrer)`（GLB-083，供各配置检查调用）。

## 范围

1. 挂载选择器全部类型（AST-022，替换 T-020/T-033 中的占位）：类型列表与角标、搜索、全选/清空、候选元信息、已选摘要；只列可用资源；自建 Skill 未通过安全检查不出现。
2. 能力页签（AST-063–066）全部类型；智能体类显示“路由目标”；卸载被路由规则引用的能力时确认（AST-065，规则失效由 T-058 处理）。
3. 删除/停用资源时返回引用方数量，前端确认文案按 PRD（AGT-005、CAP-007、KNW-042）。
4. 助理“依赖”页签（AST-070）：引用本助理的渠道、团队、外部流程；只读，可筛选状态，点击跳转。
5. CAP-053：能力（Skill/MCP/API）变更对引用它的助理**立即生效**，不随助理版本锁定；在 architecture.md 记录这一例外。

## 验收标准

- [ ] 测试：每类资源的状态映射；引用重算后旧引用消失；被引用对象删除后 `invalid_references` 返回该项；跨租户资源不出现在候选中。
