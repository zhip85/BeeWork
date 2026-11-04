import { useTranslation } from 'react-i18next'

/** Open API access instructions for an assistant (T-028): endpoint, authentication, request and response examples. */
export function OpenApiCard({ assistantId }: { assistantId: string }) {
  const { t } = useTranslation('channels')
  const base = '${API_BASE}'
  const request = `curl -X POST "${base}/v1/assistants/${assistantId}/chat" \\
  -H "Authorization: Bearer bw_live_xxxxxxxxxxxxxxxxxxxxxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "你好", "end_user": {"id": "user-123", "name": "张三"}}'`
  const response = `{
  "conversation_id": "…",
  "message": { "role": "assistant", "content": "您好，请问有什么可以帮您？" },
  "usage": { "input_tokens": 32, "output_tokens": 18 }
}`
  return (
    <div className="detail-card open-api-card">
      <h2>{t('openApi.title')}</h2>
      <p>{t('openApi.intro')}</p>
      <dl className="basic-config">
        <dt>{t('openApi.endpoint')}</dt><dd><code>POST /v1/assistants/{assistantId}/chat</code></dd>
        <dt>{t('openApi.auth')}</dt><dd><code>Authorization: Bearer bw_live_…</code> <small>{t('openApi.authHint')}</small></dd>
        <dt>{t('openApi.versions')}</dt><dd>{t('openApi.versionsHint')}</dd>
        <dt>{t('openApi.limits')}</dt><dd>{t('openApi.limitsHint')}</dd>
      </dl>
      <h3>{t('openApi.request')}</h3>
      <pre className="version-lines">{request}</pre>
      <h3>{t('openApi.response')}</h3>
      <pre className="version-lines">{response}</pre>
      <p className="wizard-muted">{t('openApi.docs')} <a href="/v1/docs" rel="noreferrer" target="_blank">/v1/docs</a>{' · '}{t('openApi.keysHint')}</p>
    </div>
  )
}
