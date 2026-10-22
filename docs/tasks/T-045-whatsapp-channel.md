# T-045 WhatsApp 渠道

**版本**：V0.3　**依赖**：T-041、T-043　**PRD**：3.7.1 WhatsApp、CHN-026、5.5 第 1 步、PLT-081（IM 渠道权益）
**外部条件**：Meta WhatsApp Business Cloud API 测试账号（Phone number ID、Access token、App secret）

## 范围

1. 适配器：连接参数 Phone number ID、Access token（加密）、App secret（加密，用于签名校验）、Webhook 校验 Token（平台生成）。
2. Webhook `/v1/channels/whatsapp/{instance_id}/webhook`：GET 订阅校验；POST 校验 `X-Hub-Signature-256`、按消息 ID 去重（防重放）、立即 200 并入队处理（T-002），回复通过 Graph API 发送（经 T-071 的安全出站客户端，目标主机固定为 graph.facebook.com）。
3. 渲染：文本、图片、快捷回复（interactive buttons，最多 3 个）、模板卡片；不支持的组件按兼容矩阵降级为文本；不流式。
4. 终端用户 = WhatsApp 号码（存哈希 + 掩码，原号码加密）；24 小时会话窗口外只能发模板消息，超出窗口的回复记录失败原因。
5. 权益：IM 渠道（专业版及以上）。

## 验收标准

- [ ] 签名错误 401；重复消息只处理一次；用 Meta 测试号跑通一次真实收发（有账号时），否则如实标注“仅契约测试”。
