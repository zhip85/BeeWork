import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bot, LayoutGrid, List, MoreHorizontal, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantMembership } from '../../types'
import { AssistantWizard } from './AssistantWizard'
import type { Assistant, AssistantPage, AssistantSummary, OrganizationOption } from './types'

type PendingAction = { kind: 'delete' | 'disable'; assistant: Assistant }
type WizardState = { mode: 'create' } | { mode: 'edit'; assistant: Assistant } | null

const PAGE_SIZE = 100

export function AssistantsPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('assistants')
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const can = (permission: string) => writable && tenant.permissions.includes(permission)

  const [items, setItems] = useState<Assistant[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<AssistantSummary | null>(null)
  const [organizations, setOrganizations] = useState<OrganizationOption[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [orgFilter, setOrgFilter] = useState<string[]>([])
  const [sort, setSort] = useState('updated_desc')
  const [view, setView] = useState<'cards' | 'list'>('cards')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [wizard, setWizard] = useState<WizardState>(null)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [reload, setReload] = useState(0)

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), sort })
    if (search.trim()) params.set('q', search.trim())
    if (status) params.set('status', status)
    orgFilter.forEach((id) => params.append('organization_id', id))
    return params.toString()
  }, [search, status, orgFilter, sort])

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      Promise.all([
        apiRequest<AssistantPage>(`/api/v1/assistants?${query}`, {}, tenantId),
        apiRequest<AssistantSummary>('/api/v1/assistants/summary', {}, tenantId),
      ])
        .then(([page, stats]) => {
          if (!active) return
          setItems(page.items)
          setTotal(page.total)
          setSummary(stats)
          setError('')
        })
        .catch((requestError: Error) => active && setError(requestError.message))
        .finally(() => active && setLoading(false))
    }, 200)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [query, tenantId, reload])

  useEffect(() => {
    let active = true
    apiRequest<{ organizations: (OrganizationOption & { parent_id: string | null })[] }>('/api/v1/organizations', {}, tenantId)
      .then((result) => active && setOrganizations(result.organizations))
      .catch(() => active && setOrganizations([])) // no organizations:view: the filter and picker simply hide
    return () => { active = false }
  }, [tenantId])

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  async function run(action: () => Promise<unknown>, doneKey: string) {
    try {
      await action()
      setNotice(t(doneKey))
      setError('')
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  const post = (assistant: Assistant, path: string) =>
    apiRequest(`/api/v1/assistants/${assistant.id}/${path}`, { method: 'POST' }, tenantId)

  const quota = summary?.quota
  const quotaRatio = quota && quota.limit ? quota.used / quota.limit : 0
  const quotaTone = quotaRatio >= 1 ? 'danger' : quotaRatio >= 0.8 ? 'warn' : ''
  const emptyAll = !loading && !error && total === 0 && !search && !status && orgFilter.length === 0

  return (
    <section className="assistants-page">
      <div className="page-heading">
        <div><p className="section-kicker">{t('title')}</p><h1>{t('title')}</h1><p>{t('subtitle')}</p></div>
        {can('assistants:edit') && <button className="primary-button" onClick={() => setWizard({ mode: 'create' })} type="button"><Plus size={16} />{t('new')}</button>}
      </div>

      {summary && (
        <div className="assistants-stats">
          <div><span>{t('stats.total')}</span><strong>{summary.total}</strong></div>
          <div><span>{t('stats.published')}</span><strong>{summary.published}</strong></div>
          <div><span>{t('stats.draft')}</span><strong>{summary.draft}</strong></div>
          <div><span>{t('stats.monthlyConversations')}</span><strong>{summary.monthly_conversations ?? t('stats.notAvailable')}</strong></div>
          <div className={`assistants-quota ${quotaTone}`}>
            <span>{t('stats.quota')}</span>
            <strong>{quota?.used} / {quota?.limit ?? t('stats.unlimited')}</strong>
            {quota?.limit ? <i style={{ width: `${Math.min(100, Math.round(quotaRatio * 100))}%` }} /> : null}
          </div>
        </div>
      )}

      <div className="assistants-toolbar">
        <label className="assistants-search"><Search size={15} /><input aria-label={t('filters.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('filters.search')} value={search} /></label>
        <select aria-label={t('filters.status')} onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">{t('filters.status')}</option>
          {(['draft', 'published', 'disabled'] as const).map((key) => <option key={key} value={key}>{t(`status.${key}`)}</option>)}
        </select>
        {organizations.length > 1 && (
          <select aria-label={t('filters.organization')} onChange={(event) => setOrgFilter(event.target.value ? [event.target.value] : [])} value={orgFilter[0] ?? ''}>
            <option value="">{t('filters.organizationAll')}</option>
            {organizations.map((org) => <option key={org.id} value={org.id}>{'  '.repeat(org.depth)}{org.name}</option>)}
          </select>
        )}
        <select aria-label={t('filters.sort')} onChange={(event) => setSort(event.target.value)} value={sort}>
          {(['updated_desc', 'created_desc', 'name_asc'] as const).map((key) => <option key={key} value={key}>{t(`sort.${key}`)}</option>)}
        </select>
        <div aria-label={t('filters.view')} className="assistants-view-switch" role="group">
          <button aria-pressed={view === 'cards'} className={view === 'cards' ? 'selected' : ''} onClick={() => setView('cards')} title={t('filters.cards')} type="button"><LayoutGrid size={15} /></button>
          <button aria-pressed={view === 'list'} className={view === 'list' ? 'selected' : ''} onClick={() => setView('list')} title={t('filters.list')} type="button"><List size={15} /></button>
        </div>
      </div>

      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error} <button className="text-button" onClick={refresh} type="button">{t('retry')}</button></p>}
      {loading && <p>{t('loading')}</p>}

      {emptyAll && (
        <div className="assistants-empty"><Bot size={26} /><h2>{t('empty')}</h2><p>{t('emptyHint')}</p>
          {can('assistants:edit') && <button className="primary-button" onClick={() => setWizard({ mode: 'create' })} type="button"><Plus size={16} />{t('new')}</button>}
        </div>
      )}
      {!loading && !error && total === 0 && !emptyAll && <p className="assistants-empty-filtered">{t('emptyFiltered')}</p>}

      {items.length > 0 && view === 'cards' && (
        <div className="assistant-grid">
          {items.map((assistant) => (
            <article className="assistant-card" key={assistant.id}>
              <header>
                <span className="assistant-icon"><Bot size={18} /></span>
                <span className={`assistant-status ${assistant.status}`}>{t(`status.${assistant.status}`)}</span>
                <RowMenu assistant={assistant} can={can} menuFor={menuFor} onEdit={() => setWizard({ mode: 'edit', assistant })} onCopy={() => void run(() => post(assistant, 'copy'), 'toast.copied')} onToggle={() => assistant.status === 'published' ? setPending({ kind: 'disable', assistant }) : void run(() => post(assistant, 'enable'), 'toast.enabled')} onDelete={() => setPending({ kind: 'delete', assistant })} setMenuFor={setMenuFor} />
              </header>
              <h3>{assistant.name}</h3>
              <p className="assistant-description">{assistant.description}</p>
              <div className="assistant-tags"><span>{enumLabelCategory(t, assistant.category)}</span><span>{assistant.organization_name}</span></div>
              <footer>
                <small>{t('card.version', { version: assistant.draft_version, time: formatRelativeTime(assistant.updated_at) })}</small>
                {assistant.can_edit && can('assistants:edit')
                  ? <button className="text-button" onClick={() => setWizard({ mode: 'edit', assistant })} type="button">{t('menu.edit')}</button>
                  : <small>{t('card.readOnly')}</small>}
              </footer>
            </article>
          ))}
          {can('assistants:edit') && <button className="assistant-card assistant-card-new" onClick={() => setWizard({ mode: 'create' })} type="button"><Plus size={20} /><strong>{t('card.newTitle')}</strong><small>{t('card.newHint')}</small></button>}
        </div>
      )}

      {items.length > 0 && view === 'list' && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>{t('list.assistant')}</th><th>{t('list.status')}</th><th>{t('list.organization')}</th><th>{t('list.updated')}</th><th /></tr></thead>
            <tbody>
              {items.map((assistant) => (
                <tr key={assistant.id}>
                  <td><strong>{assistant.name}</strong><small className="platform-tenant-slug">v{assistant.draft_version}</small></td>
                  <td><span className={`assistant-status ${assistant.status}`}>{t(`status.${assistant.status}`)}</span></td>
                  <td>{assistant.organization_name}</td>
                  <td>{formatRelativeTime(assistant.updated_at)}</td>
                  <td><RowMenu assistant={assistant} can={can} menuFor={menuFor} onEdit={() => setWizard({ mode: 'edit', assistant })} onCopy={() => void run(() => post(assistant, 'copy'), 'toast.copied')} onToggle={() => assistant.status === 'published' ? setPending({ kind: 'disable', assistant }) : void run(() => post(assistant, 'enable'), 'toast.enabled')} onDelete={() => setPending({ kind: 'delete', assistant })} setMenuFor={setMenuFor} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {wizard && (
        <AssistantWizard
          key={wizard.mode === 'edit' ? wizard.assistant.id : 'new'}
          assistant={wizard.mode === 'edit' ? wizard.assistant : null}
          onClose={() => setWizard(null)}
          onSaved={(messageKey, close) => {
            setNotice(t(messageKey))
            refresh()
            if (close) setWizard(null)
          }}
          organizations={organizations}
          quota={summary?.quota ?? null}
          tenant={tenant}
        />
      )}

      {pending && (
        <ConfirmDialog
          body={t(pending.kind === 'delete' ? 'confirm.deleteBody' : 'confirm.disableBody', { name: pending.assistant.name })}
          danger={pending.kind === 'delete'}
          onCancel={() => setPending(null)}
          onConfirm={async () => {
            const target = pending
            setPending(null)
            if (target.kind === 'delete') {
              await run(() => apiRequest(`/api/v1/assistants/${target.assistant.id}`, { method: 'DELETE' }, tenantId), 'toast.deleted')
            } else {
              await run(() => post(target.assistant, 'disable'), 'toast.disabled')
            }
          }}
          title={t(pending.kind === 'delete' ? 'confirm.deleteTitle' : 'confirm.disableTitle')}
        />
      )}
    </section>
  )
}

function enumLabelCategory(t: (key: string) => string, category: string) {
  return t(`category.${category}`)
}

function RowMenu({
  assistant,
  can,
  menuFor,
  setMenuFor,
  onEdit,
  onCopy,
  onToggle,
  onDelete,
}: {
  assistant: Assistant
  can: (permission: string) => boolean
  menuFor: string | null
  setMenuFor: (id: string | null) => void
  onEdit: () => void
  onCopy: () => void
  onToggle: () => void
  onDelete: () => void
}) {
  const { t } = useTranslation('assistants')
  const open = menuFor === assistant.id
  const ownable = assistant.can_edit
  const entries: { key: string; label: string; run: () => void; danger?: boolean }[] = []
  if (ownable && can('assistants:edit')) entries.push({ key: 'edit', label: t('menu.edit'), run: onEdit })
  if (ownable && can('assistants:edit')) entries.push({ key: 'copy', label: t('menu.copy'), run: onCopy })
  if (ownable && can('assistants:publish') && assistant.status !== 'draft') {
    entries.push({ key: 'toggle', label: assistant.status === 'published' ? t('menu.disable') : t('menu.enable'), run: onToggle })
  }
  if (ownable && can('assistants:delete') && assistant.status !== 'published') entries.push({ key: 'delete', label: t('menu.delete'), run: onDelete, danger: true })
  if (entries.length === 0) return null
  return (
    <span className="assistant-menu">
      <button aria-expanded={open} aria-haspopup="menu" aria-label={t('menu.more')} className="icon-button" onClick={() => setMenuFor(open ? null : assistant.id)} type="button"><MoreHorizontal size={16} /></button>
      {open && (
        <ul role="menu">
          {entries.map((entry) => (
            <li key={entry.key} role="none"><button className={entry.danger ? 'danger' : ''} onClick={() => { setMenuFor(null); entry.run() }} role="menuitem" type="button">{entry.label}</button></li>
          ))}
        </ul>
      )}
    </span>
  )
}

