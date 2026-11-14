# Webhook 接收与验签

BeeWork 把平台事件以 HTTPS POST 推送到租户在“设置 → Webhook”里添加的端点。

## 请求

```
POST /你的路径 HTTP/1.1
Content-Type: application/json
User-Agent: BeeWork-Webhook/1
X-BeeWork-Event: approval.updated
X-BeeWork-Delivery: 6f0c1c52-…        # 投递 ID：同一条事件的所有重试都用同一个，可用来去重
X-BeeWork-Timestamp: 1763121600       # 发送时间（Unix 秒）
X-BeeWork-Signature: sha256=3f5a…     # HMAC-SHA256
```

正文是紧凑 JSON（键按字母排序），只含 ID 与必要摘要，不含会话正文和密钥：

```json
{"created_at":"2026-11-14T12:00:00+00:00","data":{"approval_id":"…","state":"approved"},"event":"approval.updated","id":"6f0c1c52-…"}
```

## 事件

| 事件 | 触发 | `data` 里的字段 |
| --- | --- | --- |
| `conversation.completed` | 会话结束 | `conversation_id`、`assistant_id`、`status`（resolved / abandoned）、`source`、`turns`、`ended_at` |
| `approval.updated` | 审批提交、逐级通过、通过、驳回、撤回 | `approval_id`、`state`（submitted / level_approved / approved / rejected / withdrawn）、`type`、`object_type`、`object_id`、`level` |
| `assistant.published` | 智能体新版本生效 | `assistant_id`、`version`、`version_id`、`kind` |
| `usage.threshold` | 预算用量跨过阈值（每个阈值每月一次） | `threshold`、`used`、`budget`、`month` |
| `handoff.requested` | 转人工请求（随转人工功能上线） | — |
| `webhook.test` | 在页面上点“测试” | `message`、`sent_at` |

## 验签

签名是 **HMAC-SHA256**，密钥是添加端点（或轮换密钥）时只显示一次的 `whsec_…`，被签名的内容是 **`时间戳` + `.` + 原始请求体字节**。请用**原始字节**验证，不要先解析再重新序列化；建议同时拒绝时间戳与当前时间相差太大（例如 5 分钟以上）的请求，防止重放。

Python：

```python
import hashlib
import hmac
import time


def verify(secret: str, headers: dict, body: bytes, tolerance: int = 300) -> bool:
    """headers 的键用小写；body 是收到的原始字节。"""
    timestamp = headers["x-beework-timestamp"]
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["x-beework-signature"])
```

Node.js：

```js
import { createHmac, timingSafeEqual } from 'node:crypto'

export function verify(secret, headers, body, toleranceSeconds = 300) {
  const timestamp = headers['x-beework-timestamp']
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > toleranceSeconds) return false
  const expected = 'sha256=' + createHmac('sha256', secret).update(`${timestamp}.`).update(body).digest('hex')
  const received = headers['x-beework-signature']
  return expected.length === received.length && timingSafeEqual(Buffer.from(expected), Buffer.from(received))
}
```

## 应答与重试

- 返回 **2xx** 表示收到；其他状态码、连接错误或 **10 秒内没有应答**都算失败。
- 失败后按 **1 分钟、5 分钟、30 分钟**，之后**每小时**重试，从事件产生起最多 **24 小时**，然后放弃。
- 重试时 `X-BeeWork-Delivery` 与正文不变（`X-BeeWork-Timestamp` 与签名随发送时间更新），请用投递 ID 去重。
- 端点**连续 24 小时一直失败**（中间没有成功过）会被自动停用，并通知租户管理员；修好后在页面重新启用即可。
- 页面上可以看到每个端点最近 100 次投递，并可对任意一条“重发”。
- 地址必须是公网 https；解析到内网、回环或保留地址的端点会被拒绝（保存时和每次发送时都会检查），不跟随重定向。
