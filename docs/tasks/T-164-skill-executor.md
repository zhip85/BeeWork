# T-164 Skill 执行器（步骤编排与依赖解析）

**版本**：V0.5（运行时架构 v2）　**依赖**：T-160、T-074、T-071　**PRD**：演进 PRD 23.4、7.3；`architecture-runtime-v2.md`

## 目标

Skill 除了现状的“说明型”（把 `SKILL.md` 当指令加载给模型）外，增加**步骤型**：声明式的步骤与依赖，每一步调用一个已注册的能力，由执行器调度并返回结果，使 Skill 能被 L2 直达（路径 A）或被 L3 当作一个工具调用。

## 格式（2026-11-13 产品已确认）

Skill 包增加 `skill.yaml`（与 `SKILL.md` 并存；没有它的包仍是说明型，行为不变）：

```yaml
name: refund-check
description: 核对订单是否可退款
inputs: {type: object, required: [order_no], properties: {order_no: {type: string}}}   # JSON Schema，成为注册项的 input_schema
uses: [cap:order-api/get_order, cap:policy-mcp/search_policy]                          # 声明会调用的能力（注册项的稳定标识），安装 / 检查时校验
steps:
  - id: order
    call: cap:order-api/get_order
    args: {order_id: "{{inputs.order_no}}"}
  - id: policy
    call: cap:policy-mcp/search_policy
    args: {q: "退款 {{steps.order.output.category}}"}
    depends_on: [order]
    on_error: skip          # fail（默认）| skip | retry
output: "{{steps.policy.output}}"
```

表达式只支持 `{{inputs.x}}`、`{{steps.<id>.output.<路径>}}`、字符串拼接与默认值（`| default('…')`）；**不执行任何代码**。若产品已有自己的编排格式，按它改写本节，执行器的其余部分不变。

## 范围

1. **解析与检查**（T-074 的安全检查之外追加）：`skill.yaml` 的 Schema 校验；步骤 ID 唯一、依赖无环、引用的步骤存在、表达式语法；`call` 必须在 `uses` 里且指向本租户的注册项；步骤数 ≤ 20、表达式长度、输出大小上限。检查结果进入 Skill 详情的检查项（新增“步骤编排”一项）。
2. **执行**（`SkillExecutor`，`skill` 来源的 provider 增加步骤型分支）：按依赖拓扑排序，**无依赖关系的步骤并行**（受并发上限约束）；每步经 T-071 受控执行器调用（权限、参数校验、写操作确认、凭据擦除、记录）；`on_error`：`fail` 终止并返回失败步骤与原因、`skip` 该步输出为空继续、`retry` 按注册项的重试策略（T-165）；整次执行有总超时与取消检查；步骤输出大小受限。
3. **权限与范围**：Skill 步骤只能调用它 `uses` 里声明的能力，并且**调用者（智能体）的挂载范围不放宽**——步骤调用用 Skill 声明的白名单，而不是智能体的挂载集合；每步记录里带 Skill 与步骤 ID；写操作步骤沿用二次确认（确认在 Skill 层一次性展示全部将发生的写操作）。
4. **注册**：步骤型 Skill 以一个注册项出现（`input_schema` = `inputs`，`writes` = 任一步骤为写）；说明型 Skill 仍按原方式加载。
5. **追踪**：每步一个 span（`skill.<name>.<step>`），记录耗时与结果状态，不含参数值与输出内容。
6. 前端：Skill 详情显示步骤图（只读，依赖关系用列表 / 缩进表示即可）与检查项；试运行（给定输入，显示每步状态与输出摘要）。

## 验收标准

- [ ] 测试：依赖拓扑与并行（独立步骤不互相等待）；环、缺依赖、越权的 `call`、表达式错误被拒；`on_error` 三种行为；总超时与取消；写操作二次确认；步骤只能调用声明的能力（伪造 `call` 被拒）；输出大小上限；说明型 Skill 不受影响。
- [ ] 路径 A / L3 都能调用步骤型 Skill 并得到整合后的输出。

## 需要产品确认

> **2026-11-13 产品已确认**：采用上面的 `skill.yaml` 格式；步骤只做 DAG，不做条件分支和循环。下面两条保留作记录。

- `skill.yaml` 的格式是否采用（见上）；步骤是否需要条件分支 / 循环（本任务不做，只做 DAG）。
