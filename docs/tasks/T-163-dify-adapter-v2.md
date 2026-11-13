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

## 完成记录（2026-11-13，Claude Sonnet 5.5）

分支 `task/T-163-dify-adapter-v2`。迁移 `0053_dify_adapter_v2`（`flow_connections` 加 `app_mode`、`parameters`、`parameters_synced_at`、`parameters_error`、`sticky`、`idle_ttl_minutes`、`exit_words`；新表 `dify_conversations`；PostgreSQL 升级 → `alembic check` 无差异 → 降级 → 升级已验证）。**注意：`task/T-008-email-delivery` 分支也用了 `0053`（`0053_mail_deliveries`），两者合入时必须重编号（后合入的改成 `0054` 并把 `down_revision` 指向先合入的）。**

**后端**
- **应用元信息与参数**：保存 / “重新读取参数”（`POST /flow-connections/{id}/sync`）时读 Dify 的 `/info` 与 `/parameters`：校验应用类型与连接类型一致（Chatflow = `advanced-chat` / `chat`，Workflow = `workflow`；不一致以 `flow_connection.kind_mismatch` 拒绝并说明实际类型）；把 `user_input_form` 规整为变量列表（文本 / 段落 / 下拉 / 数字；文件输入不支持，略去）；工具的输入 Schema（也就是注册项的 `input_schema`，T-160 的派生自动更新）来自这些变量，`input_mapping` 仍可手动覆盖；调用时只带应用自己的变量。读取失败（不通、Key 不对）不阻止保存，原因记在连接上（`parameters_error`）。读取是幂等的，网络 / 超时 / 5xx / 429 自动重试两次。
- **会话绑定与粘性**：Chatflow 调用带上 BeeWork 会话已绑定的 Dify 会话 ID（`dify_conversations`），闲置超过 `idle_ttl_minutes`（默认 10）则开新会话。路径 A 直答 Chatflow 后在 `route.pending` 记下注册项；下一条消息进来时 `load_pending` 让连接决定：粘性开启、仍可用、绑定有效且未闲置、消息不是退出词（默认“退出 / 取消 / 结束”，忽略标点空白，命中则关闭绑定）。
- **流式**：连接开了 `streaming`，或调用方要逐字内容时，用 SSE 边收边解析：Chatflow 的 `message` 事件逐字推给调用方，Workflow 只取 `workflow_finished` 的最终输出；`error` 事件按 Dify 错误分类、流中断为 `tool.stream_interrupted`；运行被取消 / 超时时调用 Dify 的停止接口（`/chat-messages/{task_id}/stop`、`/workflows/tasks/{task_id}/stop`）。路径 A 的 Dify 回答就是回复时，token 直接推给用户（输出规则需要改写回复时不推）。
- **用户标识**：Dify 的 `user` = 租户 + 终端用户 ID 的 SHA-256 前 32 位，同一用户跨会话一致，不含邮箱 / 手机号。

**前端**：流程连接表单显示读取到的应用类型 / 输入变量 / 上次读取时间与读取错误，“重新读取参数”按钮；Chatflow 的粘性开关、闲置分钟数、退出词；流式开关的说明改为真实行为。中英文案。

**验证**：ruff；`test_dify_adapter` 12 项（`MockTransport`：变量→Schema→调用；映射覆盖；类型不一致被拒；读取失败不阻止保存与重新读取；读取重试；用户哈希稳定且不含明文；会话绑定复用与闲置新开；退出词 / 闲置 / 开关结束粘性；Chatflow 逐字流式；Workflow 只返回最终输出；错误事件、断流与失败的 Workflow；取消时停止 Dify）；T-075 与 T-161 既有用例随新请求（读取 `/info`、`/parameters`）适配；前端 tsc / lint / i18n / style；全量后端见看板。

**未完成 / 遗留**
1. **真实 Dify 验证（2026-11-13，服务层）**：用开发库里的 Chatflow 连接“aa”（Dify Cloud）直接调用本任务的服务代码：读取 `/info` 得 `advanced-chat`、`/parameters` 得 0 个输入变量；流式对话 7.9 秒、26 个 token 逐字到达调用方；带上返回的 Dify 会话 ID 发第二句，Dify 记得上一句（多轮绑定有效）；阻塞模式同样可用。**没有经过平台的完整运行链路**（开发栈当时挂载的是 T-008 的目录，没有本任务的代码），也**没有真实 Workflow 应用**，所以 Workflow 的参数读取与流式、经智能体路径 A 的粘性只有自动化测试。本机代理把 `api.dify.ai` 解析成保留地址（`198.18.x`），平台的公网地址检查按设计拒绝它；验证时用 DNS over HTTPS 查到的真实公网地址通过解析器测试接口传入，检查本身没有关闭。
2. “该流程返回结束标志则解除粘性”未做：Dify 对话流没有通用的结束标志，现在只有退出词与闲置时间两种结束方式。
3. 经 L3（路径 B）调用 Chatflow 时不会写 `route.pending`，只有路径 A 直答才会让下一条消息自动回到该流程；会话绑定（Dify 会话复用）两条路径都有。
4. 发消息类调用的重试策略留给 T-165；本任务只对读取类调用重试。
5. 页面未目视验收。

## 代码审查与修复（2026-11-13）

审查（`/code-review high`，范围 `0809e84..46a10b4`）发现并已修复：
1. **注册项的输入 Schema 不随“重新读取参数”更新**：派生监听器没有监听 `FlowConnection.parameters`，只在新建时才对。已加入监听；测试覆盖“重新读取 → Schema 更新”。
2. **流被截断仍当作成功**：Chatflow 收到部分文字后断流不报错，半截回答被当作回复。现在没有 `message_end` 一律是 `tool.stream_interrupted`。
3. **流式已推给用户后失败又回退到智能体**：用户会先看到半截回答，再看到另一个回答。现在已经推过 token 的失败不再回退，已发出的内容就是回复，路由记录标 `partial`。
4. **换了应用 / Key 后旧变量仍在**：读取失败时旧的 `parameters` 与 `app_mode` 继续生效。现在更换 Base URL、App ID 或 Key 会先清空，再读取。
5. **名为 `query` 的 Dify 变量遮蔽了提问**：Schema 里 `required` 重复且问题被当作变量转发。现在 Chatflow 里这个名字不作为变量。
6. SSE 的 `error` 事件 `status` 不是数字时抛 `ValueError`：改为按 500 分类。
7. SSE 解析不处理 CRLF 与最后一帧没有空行：已处理。
8. 流式读取因超时 / 超大小上限失败时没有通知 Dify 停止：已补。
9. `load_pending` 里用字符串查主键：改为 `UUID(...)` 并容错。

**未改**：`load_pending` 在读取辅助函数里提交（退出词会关闭绑定）——这是有意为之（退出词在路由决定之前生效），但命名上容易误解；以后重构时再改名。
