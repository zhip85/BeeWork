import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Plus, Search, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { AgentIcon } from './AgentIcon'
import { KINDS, parseKind } from './types'
import type { AgentCounts, AgentKind, AgentPage, AgentReferences, AgentSummary } from './types'

/** Agents list (AGT-001–007): one page for the three kinds, selected by the ``type`` query parameter. */
export function AgentsPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('agents')
  const [params, setParams] = useSearchParams()
  const kind = parseKind(params.get('type'))
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && tenant.permissions.includes('agents:edit')
  const canDelete = writable && tenant.permissions.includes('agents:delete')
  const [items, setItems] = useState<AgentSummary[]>([])
  const [counts, setCounts] = useState<AgentCounts | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [corrupted, setCorrupted] = useState(false)
  const [deleting, setDeleting] = useState<{ agent: AgentSummary; references: number } | null>(null)

  const load = useCallback(() => {
    const query = new URLSearchParams({ kind, limit: '100' })
    if (search.trim()) query.set('q', search.trim())
    Promise.all([
      apiRequest<AgentPage>(`/api/v1/agents?${query}`, {}, tenant.id),
      apiRequest<AgentCounts>('/api/v1/agents/summary', {}, tenant.id),
    ])
      .then(([page, summary]) => { setItems(page.items); setCounts(summary); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [kind, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  function selectKind(next: AgentKind) {
    setSearch('')
    setCorrupted(false)
    setNotice('')
    setParams(next === 'agent' ? {} : { type: next })
  }

  async function askDelete(agent: AgentSummary) {
    try {
      const references = await apiRequest<AgentReferences>(`/api/v1/agents/${agent.id}/references`, {}, tenant.id)
      setDeleting({ agent, references: references.total })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function confirmDelete() {
    if (!deleting) return
    try {
      await apiRequest(`/api/v1/agents/${deleting.agent.id}`, { method: 'DELETE' }, tenant.id)
      setNotice(t('deleted'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setDeleting(null)
  }

  const kindName = t(`kind.${kind}`)
  const createLink = kind === 'agent' ? '/agents/new' : `/agents/new?type=${kind}`

  return (
    <section className="agents-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">AI WORKSPACE</span>
          <h1>{t(`pageTitle.${kind}`)}</h1>
          <p>{t(`pageSubtitle.${kind}`)}</p>
        </div>
        {canEdit && <Link className="primary-button" to={createLink}><Plus size={16} /> {t('create', { kind: kindName })}</Link>}
      </div>
      <div className="agents-kind-tabs" role="tablist">
        {KINDS.map((value) => (
          <button aria-selected={kind === value} className={kind === value ? 'selected' : ''} key={value} onClick={() => selectKind(value)} role="tab" type="button">
            <strong>{t(`kind.${value}`)}</strong>
            <b>{counts ? counts[value] : '–'}</b>
            <small>{t(`kindHint.${value}`)}</small>
          </button>
        ))}
      </div>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('search', { kind: kindName })} onChange={(event) => setSearch(event.target.value)} placeholder={t('search', { kind: kindName })} value={search} /></label>
      </div>
      {corrupted && <p className="form-error" role="alert"><AlertTriangle size={14} /> {t('corrupted')}</p>}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      <div className="agents-grid">
        {items.map((agent) => (
          <article className="agents-card" key={agent.id}>
            <header>
              <span className={`agents-icon ${agent.kind}`}><AgentIcon name={agent.icon} /></span>
              <span className={`assistant-status agents-status-${agent.status}`}>{t(`status.${agent.status}`)}</span>
            </header>
            {agent.config_corrupted ? (
              <button className="agents-card-body" onClick={() => setCorrupted(true)} type="button">
                <h3>{agent.name}</h3>
                <p className="form-error">{t('corruptedShort')}</p>
              </button>
            ) : (
              <Link className="agents-card-body" to={`/agents/${agent.id}`}>
                <h3>{agent.name}</h3>
                <p>{agent.description || t(`defaultDescription.${agent.kind === 'agent' ? 'agent' : 'flow'}`)}</p>
              </Link>
            )}
            <div className="agents-meta">
              <span className="agents-tag">{agent.is_team ? t('team') : t(`kind.${agent.kind}`)}</span>
              <span>{agent.kind === 'agent' ? (agent.model_name ?? t('noModel')) : t('nodeOrchestration')}</span>
            </div>
            <footer>
              {agent.config_corrupted ? <span /> : <Link to={`/agents/${agent.id}`}>{t('configure')}</Link>}
              {canDelete && (
                <button aria-label={t('deleteAria', { name: agent.name })} className="icon-button" onClick={() => void askDelete(agent)} type="button"><Trash2 size={15} /></button>
              )}
            </footer>
          </article>
        ))}
      </div>
      {!loading && !error && items.length === 0 && (
        <div className="agents-empty">
          <p>{search.trim() ? t('emptyFiltered') : t('empty', { kind: kindName })}</p>
          {canEdit && !search.trim() && <Link className="primary-button" to={createLink}><Plus size={16} /> {t('create', { kind: kindName })}</Link>}
        </div>
      )}
      {deleting && (
        <ConfirmDialog
          body={deleting.references > 0 ? t('deleteBodyReferenced', { kind: kindName, count: deleting.references }) : t('deleteBody', { kind: kindName })}
          danger
          onCancel={() => setDeleting(null)}
          onConfirm={() => confirmDelete()}
          title={t('deleteTitle', { kind: kindName })}
        />
      )}
    </section>
  )
}
