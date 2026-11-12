# T-163 Dify 适配器 v2（取代自研画布）

**版本**：V0.5（运行时架构 v2）　**依赖**：T-075、T-160　**PRD**：演进 PRD 23.3、8.3、8.4；`architecture-runtime-v2.md`

## 目标

自研画布取消后，Dify 是 BeeWork 唯一的流程引擎。把 T-075 的“能调一下”补成可上线的适配器：多轮会话绑定、流式、应用参数读取、错误与重试，并去掉“本平台画布”来源。

## 范围

1. **去掉 native 来源**：`flow_connections` 只保留 `source_type=dify`；接口、前端（来源二选一的选项与画布下拉）、服务、挂载解析、`FlowConnectionProvider` 里的 native 分支与失效推导（画布停用 / 删除）全部移除；`flow_id` 字段与外键留给 T-169 的迁移一并删除。已有 native 连接数据（开发库）在迁移里标记失效。
2. **应用元信息与参数**：保存 / 测试连接时读取 Dify 的应用信息与参数定义（`/info`、`/parameters` 或当时 Dify 版本对应的接口），据此：校验应用类型（Chatflow / Workflow）与连接类型一致（不一致给出明确原因）；生成**输入 Schema**（变量名、类型、必填、选项、默认值）写入注册项（T-160），并替代手填的输入映射（映射仍可手动覆盖）；供门户表单模式（T-144）渲染表单。
3. **会话绑定**（Chatflow 多轮）：新表 `dify_conversations`（`tenant_id`、`connection_id`、BeeWork `conversation_id`、`dify_conversation_id`、最近活动时间、状态）。BeeWork 会话里调用某个 Chatflow 时带上已绑定的 Dify 会话 ID；新会话建立绑定。**粘性**：连接可配置 `sticky`（默认开）与 `idle_ttl_minutes`（默认 10）——绑定期内，该会话的下一条消息直接回到同一个 Chatflow（写入 `runs.output.route.pending`，供 T-161 读取），遇到退出词（可配置，默认“退出 / 取消 / 结束”）、超过闲置时间或该流程返回结束标志则解除。
4. **流式**：支持 Dify 的流式响应（SSE）：Chatflow 的回答以 token 事件推给用户；Workflow 用 `workflows/run` 流式时只推最终输出；断流、超时、取消的处理（取消时调用 Dify 的停止接口）。`streaming` 开关从“只保存配置”变为真正生效。
5. **用户标识**：Dify 的 `user` 用终端用户 ID 的稳定哈希（不泄露邮箱 / 手机号），同一用户跨会话一致。
6. **错误与重试**：沿用 T-075 的分类并补充：网络 / 超时 / 5xx / 429 可重试（读取参数、`/info` 等幂等调用自动重试；发消息类调用只在“请求未送达”时重试，由 T-165 的策略统一管理）；Dify 返回的错误信息不原样给用户。
7. 前端：流程连接页去掉来源选择；显示读取到的参数 / 应用类型 / 最近同步；粘性与闲置时间、退出词配置；“重新读取参数”按钮。

## 验收标准

- [ ] 测试（`httpx.MockTransport`）：参数读取生成 Schema；类型不一致被拒；多轮会话绑定复用 Dify 会话 ID；粘性与退出 / 闲置解除；流式事件顺序与取消；错误分类；`user` 哈希稳定且不含明文。
- [ ] 迁移可降级；既有 native 连接被标记失效且不再出现在候选里。
- [ ] 用真实 Dify 测试应用（一个 Chatflow、一个 Workflow）跑通“读取参数 → 测试 → 多轮 → 流式”，记录在 development-status（无应用时如实写明）。

## 外部条件

Dify 应用 API Key（Chatflow 与 Workflow 各一个）。

## 进展（2026-11-12）

范围第 1 项“去掉 native 来源”已随 `T-169` 完成（接口与前端不再提供本平台画布来源；遗留的 native 连接显示为失效，可改成 Dify 连接；`flow_connections.flow_id` 已在迁移 0051 删除）。其余各项仍待做。
