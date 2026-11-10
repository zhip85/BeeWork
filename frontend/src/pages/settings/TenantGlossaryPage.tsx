import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Pencil, Plus, RotateCcw, Search, Trash2, Upload } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequestWithStatus } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { TermImportDialog } from '../../components/glossary/TermImportDialog'
import type { ImportResult } from '../../components/glossary/TermImportDialog'
import { SettingsTabs } from './SettingsTabs'

type Item = {
  key: string
  term: string
  aliases: string[]
  definition: string
  label: 'industry_inherited' | 'tenant' | 'tenant_modified'
  restorable: boolean
  pending_approval_id: string | null
}
type Tag = { id: string; name: string; industry_name: string; term_count: number; selected: boolean; position: number | null }
type State = {
  tenant_name: string
  chain: { industries: string[]; industry_terms: number; inherited: number; tenant_level: number; total: number; hidden: number }
  glossaries: Tag[]
  settings_version: number
  items: Item[]
  approval: { required: boolean }
}
type Mutation = { outcome: 'applied' | 'pending'; kind: string }
type Draft = { key: string | null; term: string; aliases: string; definition: string; inherited: boolean }
const SCOPES = ['all', 'tenant'] as const

/** Settings → glossary (SET-060–063): inherit industry glossaries, override / hide inherited terms, add own terms, import. */
export function TenantGlossaryPage({ tenantId, canEdit, canDelete }: { tenantId: string; canEdit: boolean; canDelete: boolean }) {
  const { t } = useTranslation('glossary')
  const [state, setState] = useState<State | null>(null)
  const [scope, setScope] = useState<(typeof SCOPES)[number]>('all')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<Item | null>(null)
  const [importing, setImporting] = useState(false)

  const load = useCallback(() => {
    const params = new URLSearchParams({ scope })
    if (search.trim()) params.set('q', search.trim())
    apiRequestWithStatus<State>(`/api/v1/tenant/glossary?${params}`, {}, tenantId)
      .then((result) => { setState(result.data); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [scope, search, tenantId, t])
  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  const pendingNotice = t('tenant.submittedForApproval')

  /** Run a change and say what happened: applied (with the PRD's toast) or waiting for approval. */
  async function run(path: string, method: string, body: unknown, fallbackKind: string) {
    const result = await apiRequestWithStatus<Mutation>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) }, tenantId)
    const kind = result.data.kind === 'override_created' ? fallbackKind : result.data.kind
    setNotice(result.data.outcome === 'pending' ? pendingNotice : t(`tenant.toast.${kind}`, { defaultValue: t('termSaved') }))
    load()
  }
  async function guard(action: () => Promise<void>) {
    setError('')
    try {
      await action()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  function open(item: Item | null) {
    setFormError('')
    setDraft(item ? { key: item.key, term: item.term, aliases: item.aliases.join('；'), definition: item.definition, inherited: item.label === 'industry_inherited' } : { key: null, term: '', aliases: '', definition: '', inherited: false })
  }

  async function save() {
    if (!draft) return
    setSaving(true)
    setFormError('')
    try {
      const body = { term: draft.term, aliases: draft.aliases.split(/[,，;；]/).map((item) => item.trim()).filter(Boolean), definition: draft.definition }
      await run(draft.key ? `/api/v1/tenant/glossary/terms/${draft.key}` : '/api/v1/tenant/glossary/terms', draft.key ? 'PUT' : 'POST', body, 'savedAsTenant')
      setDraft(null)
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  const select = (ids: string[]) => guard(() => run('/api/v1/tenant/glossary/settings', 'PUT', { glossary_ids: ids, row_version: state?.settings_version ?? 0 }, 'settings'))
  const selectedIds = (state?.glossaries ?? []).filter((tag) => tag.selected).sort((a, b) => (a.position ?? 0) - (b.position ?? 0)).map((tag) => tag.id)
  const toggleTag = (tag: Tag) => select(tag.selected ? selectedIds.filter((id) => id !== tag.id) : [...selectedIds, tag.id])
  function move(tag: Tag, delta: number) {
    const index = selectedIds.indexOf(tag.id)
    const target = index + delta
    if (index < 0 || target < 0 || target >= selectedIds.length) return
    const next = [...selectedIds]
    ;[next[index], next[target]] = [next[target], next[index]]
    void select(next)
  }
  const restore = (item: Item) => guard(() => run(`/api/v1/tenant/glossary/terms/${item.key}/restore`, 'POST', undefined, 'restored'))
  const remove = (item: Item) => guard(() => run(`/api/v1/tenant/glossary/terms/${item.key}`, 'DELETE', undefined, 'hidden'))
  const restoreHidden = () => guard(() => run('/api/v1/tenant/glossary/restore-hidden', 'POST', undefined, 'restored_hidden'))

  function imported(result: ImportResult) {
    setImporting(false)
    setNotice(result.outcome === 'pending' ? pendingNotice : t('imported', { count: (result.recognized ?? 0) }))
    load()
  }

  const chain = state?.chain
  return (
    <section className="settings-page platform-governance-page">
      <SettingsTabs />
      <div className="page-heading">
        <div>
          <p className="section-kicker">{t('tenant.kicker')}</p>
          <h1>{t('tenant.title')}</h1>
          <p>{t('tenant.subtitle')}</p>
        </div>
        {canEdit && (
          <div className="plans-actions">
            <button className="text-button" onClick={() => setImporting(true)} type="button"><Upload size={14} /> {t('import')}</button>
            <button className="primary-button" onClick={() => open(null)} type="button"><Plus size={15} /> {t('tenant.new')}</button>
          </div>
        )}
      </div>
      {notice && <p className="tenant-success" role="status">{notice}{' '}{notice === pendingNotice && <Link to="/approvals">{t('tenant.viewApproval')}</Link>}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      {state?.approval.required && <p className="usage-hint">{t('tenant.approvalHint')}</p>}

      {chain && (
        <ol aria-label={t('tenant.chainLabel')} className="governance-chain">
          <li><strong>{t('tenant.chainIndustry', { names: chain.industries.length ? chain.industries.join('、') : t('tenant.none') })}</strong><small>{t('tenant.chainIndustryCount', { count: chain.industry_terms })}</small></li>
          <li><strong>{t('tenant.chainTenant', { name: state?.tenant_name })}</strong><small>{t('tenant.chainTenantCounts', { inherited: chain.inherited, own: chain.tenant_level, total: chain.total })}</small></li>
        </ol>
      )}

      {state && (
        <div className="panel glossary-tags">
          <h2>{t('tenant.inheritTitle')}</h2>
          <p className="usage-hint">{t('tenant.inheritHint')}</p>
          <div className="glossary-tag-list">
            {state.glossaries.map((tag) => (
              <span className={`glossary-tag${tag.selected ? ' selected' : ''}`} key={tag.id}>
                <button aria-pressed={tag.selected} disabled={!canEdit} onClick={() => void toggleTag(tag)} type="button">
                  {tag.selected && <b>{tag.position}</b>}{tag.industry_name} <small>{t('termCount', { count: tag.term_count })}</small>
                </button>
                {tag.selected && canEdit && selectedIds.length > 1 && (
                  <>
                    <button aria-label={t('tenant.moveEarlier')} disabled={tag.position === 1} onClick={() => move(tag, -1)} title={t('tenant.moveEarlier')} type="button"><ArrowLeft size={12} /></button>
                    <button aria-label={t('tenant.moveLater')} disabled={tag.position === selectedIds.length} onClick={() => move(tag, 1)} title={t('tenant.moveLater')} type="button"><ArrowRight size={12} /></button>
                  </>
                )}
              </span>
            ))}
          </div>
          {selectedIds.length > 1 && <p className="usage-hint">{t('tenant.orderHint')}</p>}
        </div>
      )}

      <div className="panel">
        <div className="detail-card-head">
          <div><h2>{t('tenant.termsTitle')}</h2><p className="usage-hint">{t('tenant.termsHint')}</p></div>
        </div>
        <div className="knowledge-status-bar">
          {SCOPES.map((value) => (
            <button aria-pressed={scope === value} className={scope === value ? 'active' : ''} key={value} onClick={() => setScope(value)} type="button">{t(`tenant.scope.${value}`)}</button>
          ))}
          <label className="search-field"><Search size={14} /><input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></label>
        </div>
        {chain && chain.hidden > 0 && (
          <p className="assistants-notice">
            {t('tenant.hiddenCount', { count: chain.hidden })}{' '}
            {canEdit && <button className="text-button" onClick={() => void restoreHidden()} type="button">{t('tenant.restoreAll')}</button>}
          </p>
        )}
        {state && state.items.length === 0 && <p className="assistants-empty-filtered">{search ? t('noMatch') : t('tenant.empty')}</p>}
        {state && state.items.length > 0 && (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>{t('col.term')}</th><th>{t('col.aliases')}</th><th>{t('col.definition')}</th><th>{t('col.level')}</th><th /></tr></thead>
              <tbody>
                {state.items.map((item) => (
                  <tr key={item.key}>
                    <td><strong>{item.term}</strong>{item.pending_approval_id && <small className="tenant-status-note">{t('tenant.pending')}</small>}</td>
                    <td>{item.aliases.length ? item.aliases.join('、') : '—'}</td>
                    <td className="glossary-definition">{item.definition || '—'}</td>
                    <td><span className={`assistant-status ${item.label === 'industry_inherited' ? 'draft' : 'published'}`}>{t(`tenant.layer.${item.label}`)}</span></td>
                    <td className="member-actions-cell">
                      {canEdit && <button aria-label={t('edit')} className="icon-button" disabled={!!item.pending_approval_id} onClick={() => open(item)} title={t('edit')} type="button"><Pencil size={15} /></button>}
                      {canEdit && item.restorable && <button aria-label={t('tenant.restore')} className="icon-button" disabled={!!item.pending_approval_id} onClick={() => void restore(item)} title={t('tenant.restore')} type="button"><RotateCcw size={15} /></button>}
                      {canDelete && <button aria-label={t('delete')} className="icon-button" disabled={!!item.pending_approval_id} onClick={() => setDeleting(item)} title={t('delete')} type="button"><Trash2 size={15} /></button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {draft && (
        <div className="modal-backdrop" onClick={() => !saving && setDraft(null)} role="presentation">
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void save() }} role="dialog" aria-modal="true">
            <div className="section-kicker">TENANT</div>
            <h2>{t(draft.key ? 'editTerm' : 'newTerm')}</h2>
            {draft.inherited && <p className="assistants-notice">{t('tenant.inheritedNotice')}</p>}
            <label>{t('field.term')} *<input autoFocus maxLength={200} onChange={(event) => setDraft({ ...draft, term: event.target.value })} required value={draft.term} /></label>
            <label>{t('field.aliases')}<input onChange={(event) => setDraft({ ...draft, aliases: event.target.value })} value={draft.aliases} /><small>{t('field.aliasesHint')}</small></label>
            <label>{t('field.definition')}<textarea maxLength={1000} onChange={(event) => setDraft({ ...draft, definition: event.target.value })} rows={3} value={draft.definition} /></label>
            {formError && <p className="members-error" role="alert">{formError}</p>}
            <div className="plans-actions">
              <button className="text-button" disabled={saving} onClick={() => setDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving || !draft.term.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
            </div>
          </form>
        </div>
      )}
      {deleting && (
        <ConfirmDialog
          body={t(deleting.label === 'tenant' ? 'deleteTermBody' : 'tenant.hideBody', { name: deleting.term })} danger onCancel={() => setDeleting(null)}
          onConfirm={async () => { const target = deleting; setDeleting(null); await remove(target) }} title={t('deleteTermTitle')}
        />
      )}
      {importing && <TermImportDialog kicker="TENANT" onClose={() => setImporting(false)} onDone={imported} tenantId={tenantId} url="/api/v1/tenant/glossary/terms/import" />}
    </section>
  )
}
