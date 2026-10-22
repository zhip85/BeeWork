# T-025 LangGraph 运行时 v1

**阶段**：3（核心）　**依赖**：T-002、T-020　**PRD**：5.5（第 1–2、4、8、10–11 步的最小子集）、4.4、NFR-001、NFR-012
**外部条件**：真实调用需供应商 Key（在平台模型目录配置）；Langfuse Cloud 的 Host/Public/Secret Key

## 目标

把助理配置编译成 LangGraph 图，在 Worker 中执行，经 LiteLLM 调用模型、经 Langfuse 记录轨迹，并通过 SSE 把事件推给调用方。这是 BeeWork 自研的核心。

## 设计

- `runtime/dsl.py`：`AssistantDSLv1`（Pydantic）：`prompt`、`model_alias`、`knowledge`（T-033 用）、`greeting`、`limits`（max_tokens、超时）。从草稿或版本快照构造。
- `runtime/compiler.py`：`compile_assistant(dsl) -> CompiledGraph`。v1 图：`input_guard`（最小检查：长度、空消息；PII 留给 E-50）→ `retrieve`（T-033 前为空操作）→ `generate`（ChatOpenAI → LiteLLM）→ `output`。节点只能来自注册表，不允许任意代码。
- 模型客户端：`langchain-openai` 的 `ChatOpenAI(base_url=f"{LITELLM_BASE_URL}/v1", api_key=租户环境虚拟 Key, model=model_alias, streaming=True)`；请求 `metadata`/`extra_body` 带 `tenant_id`、`assistant_id`、`version`、`run_id`、`source`，供 T-011 归因。
- 追踪：`langfuse` 的 LangChain `CallbackHandler`，`trace_id=run_id`；未配置 Langfuse 时跳过。依赖版本锁定在 `requirements.txt`。
- `runs` 表：`tenant_id`、`assistant_id`、`version_id`（草稿调试时为空 + `draft_row_version`）、`environment`、`source`（debug/test/live/api）、`status`（queued/running/succeeded/failed/cancelled）、`input`、`output`、`error`、`latency_ms`、`tokens_in/out`、`trace_url`、`created_by`。
- 流程：`POST /assistants/{id}/runs`（调试，草稿）→ 建 run + 入队 `runtime.execute` → 立即返回 `run_id`；Worker 执行并用 `publish_run_event` 推送 `token`、`step`、`done`、`error` 事件；`GET /runs/{run_id}/events`（SSE，T-002 的 `stream_run_events`）；`POST /runs/{run_id}/cancel`。
- 准入（5.5 第 2 步）：租户状态（只读/停用不服务，`tenant_access_mode`）、助理状态、环境网关同步状态、点数配额（T-010 `check_quota`）。
- 线上执行只读取 `assistant_versions.snapshot`；调试执行读取草稿。

## 验收标准

- [ ] 单元测试：DSL 校验、编译出的图结构、准入拒绝分支；模型调用用假的 ChatModel（LangChain `FakeListChatModel`）测试，不依赖网络。
- [ ] 集成验证：本地 LiteLLM + 一个真实模型（有 Key 时）跑通 SSE 流式；无 Key 时在 LiteLLM 注册一个 `litellm_params.mock_response` 的 deployment 跑通链路，并在 development-status 中如实写明用的是哪种。
- [ ] Langfuse Cloud 上能看到 trace（有 Key 时），trace 元数据含 tenant/assistant/run。
- [ ] 取消运行、超时、模型错误都有可见状态与事件。
