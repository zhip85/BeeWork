import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, MoreHorizontal, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { KINDS, READY_KINDS, parseKind } from './types'
import type { CapCounts, CapKind, Capability } from './types'

/** Capability centre (CAP-001–008): Skill, MCP and API plugins in one list, the type chosen by ``?type=``. */
export function CapabilitiesPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const kind = parseKind(params.get('type'))
  const writable = tenant.access_mode === 'full'
  const canCreate = writable && tenant.permissions.includes('capabilities:edit') && READY_KINDS.includes(kind)
  const [items, setItems] = useState<Capability[]>([])
  const [counts, setCounts] = useState<CapCounts | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<{ kind: 'delete' | 'disable'; cap: Capability; references: number } | null>(null)

  const load = useCallback(() => {
    if (!READY_KINDS.includes(kind)) { setItems([]); setLoading(false); return }
    const query = new URLSearchParams({ kind })
    if (search.trim()) query.set('q', search.trim())
    Promise.all([
      apiRequest<Capability[]>(`/api/v1/capabilities?${query}`, {}, tenant.id),
      apiRequest<CapCounts>('/api/v1/capabilities/summary', {}, tenant.id),
    ])
      .then(([list, summary]) => { setItems(list); setCounts(summary); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [kind, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  useEffect(() => {
    apiRequest<CapCounts>('/api/v1/capabilities/summary', {}, tenant.id).then(setCounts).catch(() => undefined)
  }, [tenant.id])

  useEffect(() => {
    if (!menuFor) return
    const close = () => setMenuFor(null)
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('click', close)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', onKey) }
  }, [menuFor])

  function selectKind(next: CapKind) {
    setSearch(''); setNotice(''); setLoading(true)
    setParams(next === 'mcp' ? {} : { type: next })
  }

  const open = (cap: Capability) => navigate(`/capabilities/${cap.kind}/${cap.id}`)

  async function act(cap: Capability, action: 'enable' | 'copy') {
    setMenuFor(null)
    try {
      const result = await apiRequest<Capability>(`/api/v1/capabilities/${cap.id}/${action}`, { method: 'POST' }, tenant.id)
      setNotice(action === 'copy' ? t('copied') : result.pending_approval ? t('enablePending') : t('enabled'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function ask(cap: Capability, action: 'delete' | 'disable') {
    setMenuFor(null)
    try {
      const refs = await apiRequest<{ total: number }>(`/api/v1/capabilities/${cap.id}/references`, {}, tenant.id)
      setConfirm({ kind: action, cap, references: refs.total })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function runConfirmed() {
    if (!confirm) return
    try {
      if (confirm.kind === 'delete') await apiRequest(`/api/v1/capabilities/${confirm.cap.id}`, { method: 'DELETE' }, tenant.id)
      else await apiRequest(`/api/v1/capabilities/${confirm.cap.id}/disable`, { method: 'POST' }, tenant.id)
      setNotice(t(confirm.kind === 'delete' ? 'deleted' : 'disabled'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setConfirm(null)
  }

  const kindName = t(`kind.${kind}`)

  return (
    <section className="capabilities-page">
      <div className="page-heading">
        <div><h1>{t('title')}</h1><p>{t('subtitle')}</p></div>
        {canCreate && <Link className="primary-button" to={`/capabilities/${kind}/new`}><Plus size={16} /> {t(`create.${kind}`)}</Link>}
      </div>
      <div className="flows-kind-tabs" role="tablist">
        {KINDS.map((value) => (
          <button aria-selected={kind === value} className={kind === value ? 'selected' : ''} key={value} onClick={() => selectKind(value)} role="tab" type="button">
            <strong>{t(`kind.${value}`)}</strong>
            <b>{counts ? counts[value] : '–'}</b>
            <small>{t(`kindHint.${value}`)}</small>
          </button>
        ))}
      </div>
      {!READY_KINDS.includes(kind) ? (
        <p className="assistants-empty-filtered">{t('soon', { kind: kindName })}</p>
      ) : (
        <>
          <div className="knowledge-toolbar">
            <label className="search-field"><Search size={15} /><input aria-label={t('search', { kind: kindName })} onChange={(event) => setSearch(event.target.value)} placeholder={t('search', { kind: kindName })} value={search} /></label>
          </div>
          {notice && <p className="assistants-notice" role="status">{notice}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          {loading && <p>{t('loading')}</p>}
          <div className="flows-grid">
            {items.map((cap) => (
              <article className="flows-card" key={cap.id}>
                <header>
                  <span className={`assistant-status flows-status-${cap.status === 'available' ? 'published' : cap.status}`}>{t(`status.${cap.status}`)}</span>
                  {cap.pending_approval && <span className="assistant-status draft">{t('pendingApproval')}</span>}
                  {cap.mcp?.health === 'error' && <span className="assistant-status capability-unhealthy"><AlertTriangle size={12} /> {t('unhealthy')}</span>}
                  {(cap.can_edit || cap.can_publish || cap.can_delete) && writable && (
                    <span className="assistant-menu">
                      <button aria-expanded={menuFor === cap.id} aria-haspopup="menu" aria-label={t('menu')} className="icon-button" onClick={(event) => { event.stopPropagation(); setMenuFor(menuFor === cap.id ? null : cap.id) }} type="button"><MoreHorizontal size={16} /></button>
                      {menuFor === cap.id && (
                        <ul role="menu">
                          <li role="none"><Link role="menuitem" to={`/capabilities/${cap.kind}/${cap.id}`}>{t('menuConfigure')}</Link></li>
                          {cap.can_publish && cap.status === 'available' && <li role="none"><button onClick={() => void ask(cap, 'disable')} role="menuitem" type="button">{t('menuDisable')}</button></li>}
                          {cap.can_publish && cap.status !== 'available' && <li role="none"><button onClick={() => void act(cap, 'enable')} role="menuitem" type="button">{t('menuEnable')}</button></li>}
                          {cap.can_edit && <li role="none"><button onClick={() => void act(cap, 'copy')} role="menuitem" type="button">{t('menuCopy')}</button></li>}
                          {cap.can_delete && <li role="none"><button className="danger" onClick={() => void ask(cap, 'delete')} role="menuitem" type="button">{t('menuDelete')}</button></li>}
                        </ul>
                      )}
                    </span>
                  )}
                </header>
                <button className="flows-card-body" onClick={() => open(cap)} type="button">
                  <h3>{cap.name}</h3>
                  <p>{cap.description || t(`kindHint.${cap.kind}`)}</p>
                </button>
                <div className="flows-meta"><span className="flows-tag">{t(`kind.${cap.kind}`)}</span><span>{cap.meta}</span></div>
                <footer><span>{t(`footer.${cap.status}`)}</span><button className="text-button" onClick={() => open(cap)} type="button">{t('open')}</button></footer>
              </article>
            ))}
          </div>
          {!loading && !error && items.length === 0 && (
            <div className="flows-empty">
              <p>{search.trim() ? t('emptyFiltered') : t('empty', { kind: kindName })}</p>
              {canCreate && !search.trim() && <Link className="primary-button" to={`/capabilities/${kind}/new`}><Plus size={16} /> {t(`create.${kind}`)}</Link>}
            </div>
          )}
        </>
      )}
      {confirm && (
        <ConfirmDialog
          body={confirm.references > 0 ? t(`${confirm.kind}BodyReferenced`, { count: confirm.references }) : t(`${confirm.kind}Body`)}
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={() => runConfirmed()}
          title={t(`${confirm.kind}Title`, { name: confirm.cap.name })}
        />
      )}
    </section>
  )
}
