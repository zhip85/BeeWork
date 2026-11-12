import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequest } from '../../api'

type Dependency = { kind: 'channel' | 'assistant' | 'flow' | 'portal_app'; id: string; name: string; status: string; detail: string }

/** Dependencies tab (AST-070): what depends on this agent — channels, agents that delegate to it, flows that call it,
 * portal apps. Read only; filter by status; a click opens the dependent. */
export function DependenciesTab({ assistantId, tenantId }: { assistantId: string; tenantId: string }) {
  const { t } = useTranslation('assistants')
  const [items, setItems] = useState<Dependency[] | null>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')

  useEffect(() => {
    apiRequest<{ items: Dependency[] }>(`/api/v1/assistants/${assistantId}/dependencies`, {}, tenantId)
      .then((result) => { setItems(result.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('dependencies.loadFailed')))
  }, [assistantId, tenantId, t])

  const statuses = useMemo(() => [...new Set((items ?? []).map((item) => item.status))].sort(), [items])
  const visible = (items ?? []).filter((item) => !status || item.status === status)

  function target(item: Dependency): string | null {
    if (item.kind === 'channel') return `/assistants/${assistantId}?tab=channels`
    if (item.kind === 'assistant') return `/assistants/${item.id}`
    return null
  }

  return (
    <div className="detail-card">
      <header className="detail-card-head">
        <h2>{t('dependencies.title', { count: items?.length ?? 0 })}</h2>
        {statuses.length > 1 && (
          <select aria-label={t('dependencies.filter')} onChange={(event) => setStatus(event.target.value)} value={status}>
            <option value="">{t('dependencies.allStatuses')}</option>
            {statuses.map((value) => <option key={value} value={value}>{t(`dependencies.status.${value}`, { defaultValue: value })}</option>)}
          </select>
        )}
      </header>
      <p className="wizard-muted">{t('dependencies.hint')}</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {!items && !error && <p className="wizard-muted">…</p>}
      {items && items.length === 0 && <p className="assistants-empty-filtered">{t('dependencies.empty')}</p>}
      <ul className="dependency-list">
        {visible.map((item) => {
          const to = target(item)
          return (
            <li key={`${item.kind}:${item.id}`}>
              <span className="mount-state">{t(`dependencies.kind.${item.kind}`)}</span>
              {to ? <Link to={to}>{item.name}</Link> : <span>{item.name}</span>}
              <span className="assistant-status">{t(`dependencies.status.${item.status}`, { defaultValue: item.status })}</span>
              {item.detail && <small>{item.detail}</small>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
