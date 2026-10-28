import { useEffect, useState } from 'react'
import { ArrowLeft, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { formatRelativeTime } from '../../i18n/format'

type Conversation = {
  id: string
  channel: string
  end_user_name: string | null
  turns: number
  csat: string | null
  tokens: number
  status: 'in_progress' | 'handoff' | 'resolved' | 'abandoned'
  started_at: string
  last_active_at: string
}
type Message = { id: string; seq: number; role: string; content: string; trace_id: string | null; created_at: string }
type Detail = Conversation & { messages: Message[] }
type Page = { items: Conversation[]; total: number }

export function ConversationsPage({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation('conversations')
  const { assistantId = '' } = useParams()
  const [status, setStatus] = useState('')
  const [page, setPage] = useState<Page | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let active = true
    const query = new URLSearchParams({ limit: '50' })
    if (status) query.set('status', status)
    apiRequest<Page>(`/api/v1/assistants/${assistantId}/conversations?${query}`, {}, tenantId)
      .then((result) => {
        if (!active) return
        setPage(result)
        setError('')
      })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [assistantId, tenantId, status, reload, t])

  return (
    <section className="settings-page conversations-page">
      <Link className="text-button" to="/assistants"><ArrowLeft size={14} /> {t('back')}</Link>
      <div className="page-heading"><div><p className="section-kicker">{t('title')}</p><h1>{t('title')}</h1><p>{t('subtitle')}</p></div></div>
      <div className="assistants-toolbar">
        <select aria-label={t('filter.status')} onChange={(event) => { setPage(null); setStatus(event.target.value) }} value={status}>
          <option value="">{t('filter.status')}</option>
          {(['in_progress', 'handoff', 'resolved', 'abandoned'] as const).map((key) => <option key={key} value={key}>{t(`status.${key}`)}</option>)}
        </select>
      </div>
      <p className="wizard-muted">{t('note')}</p>
      {error && <p className="form-error" role="alert">{error} <button className="text-button" onClick={() => setReload((value) => value + 1)} type="button">{t('retry')}</button></p>}
      {!page && !error && <p>{t('loading')}</p>}
      {page && page.items.length === 0 && <p className="assistants-empty-filtered">{status ? t('emptyFiltered') : t('empty')}</p>}
      {page && page.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>{t('cols.id')}</th><th>{t('cols.user')}</th><th>{t('cols.channel')}</th><th>{t('cols.turns')}</th><th>{t('cols.csat')}</th><th>{t('cols.status')}</th><th>{t('cols.active')}</th></tr></thead>
            <tbody>
              {page.items.map((item) => (
                <tr className="clickable-row" key={item.id} onClick={() => setSelected(item.id)} onKeyDown={(event) => event.key === 'Enter' && setSelected(item.id)} tabIndex={0}>
                  <td><code>{item.id.slice(0, 8)}</code></td>
                  <td>{item.end_user_name ?? '—'}</td>
                  <td>{t(`channel.${item.channel}`, { defaultValue: item.channel })}</td>
                  <td>{item.turns}</td>
                  <td>{item.csat ?? '—'}</td>
                  <td><span className={`assistant-status ${item.status}`}>{t(`status.${item.status}`)}</span></td>
                  <td>{formatRelativeTime(item.last_active_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selected && <ConversationDrawer conversationId={selected} onClose={() => setSelected(null)} tenantId={tenantId} />}
    </section>
  )
}

function ConversationDrawer({ conversationId, tenantId, onClose }: { conversationId: string; tenantId: string; onClose: () => void }) {
  const { t } = useTranslation('conversations')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<Detail>(`/api/v1/conversations/${conversationId}`, {}, tenantId)
      .then((result) => active && setDetail(result))
      .catch((requestError: Error) => active && setError(requestError.message || t('detail.loadFailed')))
    return () => { active = false }
  }, [conversationId, tenantId, t])

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <aside aria-label={t('detail.title')} aria-modal="true" className="conversation-drawer" onClick={(event) => event.stopPropagation()} role="dialog">
        <header><h2>{t('detail.title')}</h2><button aria-label={t('detail.close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button></header>
        {error && <p className="form-error">{error}</p>}
        {!detail && !error && <p>{t('detail.loading')}</p>}
        {detail && (
          <>
            <p className="wizard-muted">{detail.end_user_name ?? '—'} · {t(`status.${detail.status}`)} · {t('detail.tokens', { count: detail.tokens })}</p>
            {detail.messages.length === 0 && <p>{t('detail.noMessages')}</p>}
            <ol className="conversation-messages">
              {detail.messages.map((message) => (
                <li className={`message-${message.role}`} key={message.id}>
                  <strong>{t(`detail.role.${message.role}`, { defaultValue: message.role })}</strong>
                  <p>{message.content}</p>
                </li>
              ))}
            </ol>
          </>
        )}
      </aside>
    </div>
  )
}
