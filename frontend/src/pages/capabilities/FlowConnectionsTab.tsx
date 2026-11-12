import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, MoreHorizontal, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import type { FlowConnection, FlowConnectionList, FlowKind } from './types'

const STATUS_CLASS = { available: 'published', disabled: 'disabled', invalid: 'draft' } as const

/** The Chatflow / Workflow tabs of the capability centre (T-075): connections that wrap a published canvas of this platform
 * or a Dify application. A connection whose canvas is gone shows "失效" with the reason instead of disappearing. */
export function FlowConnectionsTab({ tenant, kind, onChanged }: { tenant: TenantMembership; kind: FlowKind; onChanged: () => void }) {
  const { t } = useTranslation('capabilities')
  const navigate = useNavigate()
  const writable = tenant.access_mode === 'full'
  const canCreate = writable && tenant.permissions.includes('capabilities:edit')
  const [items, setItems] = useState<FlowConnection[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<{ action: 'delete' | 'disable'; item: FlowConnection } | null>(null)
  const kindName = t(`kind.${kind}`)

  const load = useCallback(() => {
    const query = new URLSearchParams({ kind })
    if (search.trim()) query.set('q', search.trim())
    apiRequest<FlowConnectionList>(`/api/v1/flow-connections?${query}`, {}, tenant.id)
      .then((data) => { setItems(data.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [kind, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  useEffect(() => {
    if (!menuFor) return
    const close = () => setMenuFor(null)
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('click', close)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', onKey) }
  }, [menuFor])

  const open = (item: FlowConnection) => navigate(`/capabilities/${item.kind}/${item.id}`)
  const refresh = () => { load(); onChanged() }
  const fail = (requestError: unknown) => setError(requestError instanceof Error ? requestError.message : t('actionFailed'))

  async function enable(item: FlowConnection) {
    setMenuFor(null)
    try {
      await apiRequest(`/api/v1/flow-connections/${item.id}/enable`, { method: 'POST' }, tenant.id)
      setNotice(t('enabled')); refresh()
    } catch (requestError) { fail(requestError) }
  }

  async function runConfirmed() {
    if (!confirm) return
    try {
      if (confirm.action === 'delete') await apiRequest(`/api/v1/flow-connections/${confirm.item.id}`, { method: 'DELETE' }, tenant.id)
      else await apiRequest(`/api/v1/flow-connections/${confirm.item.id}/disable`, { method: 'POST' }, tenant.id)
      setNotice(t(confirm.action === 'delete' ? 'deleted' : 'disabled')); refresh()
    } catch (requestError) { fail(requestError) }
    setConfirm(null)
  }

  return (
    <>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('search', { kind: kindName })} onChange={(event) => setSearch(event.target.value)} placeholder={t('search', { kind: kindName })} value={search} /></label>
        {canCreate && <Link className="primary-button" to={`/capabilities/${kind}/new`}><Plus size={16} /> {t(`create.${kind}`)}</Link>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      <div className="flows-grid">
        {items.map((item) => (
          <article className="flows-card" key={item.id}>
            <header>
              <span className={`assistant-status flows-status-${STATUS_CLASS[item.state]}`}>{item.state === 'invalid' && <AlertTriangle size={12} />} {t(`flowConn.stateLabel.${item.state}`)}</span>
              {(item.can_edit || item.can_delete) && writable && (
                <span className="assistant-menu">
                  <button aria-expanded={menuFor === item.id} aria-haspopup="menu" aria-label={t('menu')} className="icon-button" onClick={(event) => { event.stopPropagation(); setMenuFor(menuFor === item.id ? null : item.id) }} type="button"><MoreHorizontal size={16} /></button>
                  {menuFor === item.id && (
                    <ul role="menu">
                      <li role="none"><Link role="menuitem" to={`/capabilities/${item.kind}/${item.id}`}>{t('menuConfigure')}</Link></li>
                      {item.status === 'enabled'
                        ? <li role="none"><button onClick={() => { setMenuFor(null); setConfirm({ action: 'disable', item }) }} role="menuitem" type="button">{t('menuDisable')}</button></li>
                        : <li role="none"><button onClick={() => void enable(item)} role="menuitem" type="button">{t('menuEnable')}</button></li>}
                      {item.can_delete && <li role="none"><button className="danger" onClick={() => { setMenuFor(null); setConfirm({ action: 'delete', item }) }} role="menuitem" type="button">{t('menuDelete')}</button></li>}
                    </ul>
                  )}
                </span>
              )}
            </header>
            <button className="flows-card-body" onClick={() => open(item)} type="button">
              <h3>{item.name}</h3>
              <p>{item.description || t(`kindHint.${item.kind}`)}</p>
            </button>
            <div className="flows-meta">
              <span className="flows-tag">{t(`flowConn.source.${item.source_type}`)}</span>
              <span>{item.source_type === 'native' ? t('flowConn.summaryNative', { flow: item.flow_name ?? '—' }) : t('flowConn.summaryDify', { host: item.host })}</span>
              {item.references > 0 && <span>{t('flowConn.references', { count: item.references })}</span>}
            </div>
            <footer><span>{item.state_reason ?? t(`flowConn.footer.${item.state}`)}</span><button className="text-button" onClick={() => open(item)} type="button">{t('open')}</button></footer>
          </article>
        ))}
      </div>
      {!loading && !error && items.length === 0 && (
        <div className="flows-empty">
          <p>{search.trim() ? t('flowConn.emptyFiltered') : t('flowConn.empty', { kind: kindName })}</p>
          {canCreate && !search.trim() && <Link className="primary-button" to={`/capabilities/${kind}/new`}><Plus size={16} /> {t(`create.${kind}`)}</Link>}
        </div>
      )}
      {confirm && (
        <ConfirmDialog
          body={confirm.item.references > 0 ? t(`flowConn.${confirm.action}BodyReferenced`, { count: confirm.item.references }) : t(`flowConn.${confirm.action}Body`)}
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={() => runConfirmed()}
          title={t(`flowConn.${confirm.action}Title`, { name: confirm.item.name })}
        />
      )}
    </>
  )
}
