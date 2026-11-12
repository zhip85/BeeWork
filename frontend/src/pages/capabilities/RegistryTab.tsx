import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { EntryKind, EntryStatus, RegistryEntry, RegistryList } from './types'

const KINDS: EntryKind[] = ['mcp_tool', 'api_tool', 'skill', 'dify_workflow', 'dify_chatflow', 'agent', 'kb']
const STATUSES: EntryStatus[] = ['available', 'disabled', 'invalid']
const RETRY_ON = ['network', 'timeout', '5xx', '429'] as const
const MAX_EXAMPLES = 10

/** Where an entry's owner is managed. Sub-agents and knowledge bases are not registered in the capability centre: they have their
 * own pages, and the registry only links to them. */
function ownerPath(entry: RegistryEntry): string {
  if (entry.owner.page === 'assistants') return `/assistants/${entry.owner.id}`
  if (entry.owner.page === 'knowledge') return `/knowledge/${entry.owner.id}`
  const tab = { mcp_tool: 'mcp', api_tool: 'api', skill: 'skill', dify_chatflow: 'chatflow', dify_workflow: 'workflow' }[entry.kind as 'mcp_tool']
  return `/capabilities/${tab}/${entry.owner.id}`
}

/** The unified capability registry view (T-160): every entry with its status, and the fields only the registry holds — intent
 * description and examples, direct calls (path A), timeout and retry. */
export function RegistryTab({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const canEdit = tenant.access_mode === 'full' && tenant.permissions.includes('capabilities:edit')
  const [items, setItems] = useState<RegistryEntry[]>([])
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState<RegistryEntry | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    const query = new URLSearchParams()
    if (kind) query.set('kind', kind)
    if (status) query.set('status', status)
    if (search.trim()) query.set('q', search.trim())
    apiRequest<RegistryList>(`/api/v1/registry/entries?${query}`, {}, tenant.id)
      .then((data) => { setItems(data.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('registry.loadFailed')))
      .finally(() => setLoading(false))
  }, [kind, status, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  async function resync() {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await apiRequest<{ entries: number }>('/api/v1/registry/resync', { method: 'POST' }, tenant.id)
      setNotice(t('registry.resynced', { count: result.entries })); load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally { setBusy(false) }
  }

  return (
    <>
      <p className="usage-hint">{t('registry.hint')}</p>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('registry.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('registry.search')} value={search} /></label>
        <select aria-label={t('registry.filterKind')} onChange={(event) => setKind(event.target.value)} value={kind}>
          <option value="">{t('registry.filterKind')}：{t('registry.all')}</option>
          {KINDS.map((value) => <option key={value} value={value}>{t(`registry.kind.${value}`)}</option>)}
        </select>
        <select aria-label={t('registry.filterStatus')} onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">{t('registry.filterStatus')}：{t('registry.all')}</option>
          {STATUSES.map((value) => <option key={value} value={value}>{t(`registry.status.${value}`)}</option>)}
        </select>
        {canEdit && <button className="secondary-button" disabled={busy} onClick={() => void resync()} type="button">{busy ? t('registry.resyncing') : t('registry.resync')}</button>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      <div className="flows-grid">
        {items.map((entry) => (
          <article className="flows-card" key={entry.id}>
            <header>
              <span className={`assistant-status flows-status-${entry.status === 'available' ? 'published' : entry.status === 'disabled' ? 'disabled' : 'draft'}`}>{t(`registry.status.${entry.status}`)}</span>
              <span className="flows-tag">{t(`registry.kind.${entry.kind}`)}</span>
              {entry.writes && <span className="flows-tag">{t('registry.writes')}</span>}
            </header>
            <button className="flows-card-body" onClick={() => setEditing(entry)} type="button">
              <h3>{entry.name}</h3>
              <p>{entry.intent_description || entry.description || t('registry.noIntent')}</p>
            </button>
            <div className="flows-meta">
              <span>{t('registry.owner', { name: entry.owner.name })}</span>
              <span>{entry.direct_enabled ? t('registry.direct') : t('registry.notDirect')}</span>
            </div>
            <footer><Link className="text-button" to={ownerPath(entry)}>{t(`registry.manageOn.${entry.owner.page}`)}</Link><button className="text-button" onClick={() => setEditing(entry)} type="button">{canEdit ? t('registry.edit') : t('registry.view')}</button></footer>
          </article>
        ))}
      </div>
      {!loading && !error && items.length === 0 && <div className="flows-empty"><p>{search.trim() || kind || status ? t('registry.emptyFiltered') : t('registry.empty')}</p></div>}
      {editing && <EntryDialog canEdit={canEdit} entry={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice(t('registry.dialog.saved')); load() }} tenantId={tenant.id} />}
    </>
  )
}

function EntryDialog({ entry, canEdit, tenantId, onClose, onSaved }: { entry: RegistryEntry; canEdit: boolean; tenantId: string; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation('capabilities')
  const [intent, setIntent] = useState(entry.intent_description)
  const [examples, setExamples] = useState(entry.intent_examples.join('\n'))
  const [direct, setDirect] = useState(entry.direct_enabled)
  const [directOutput, setDirectOutput] = useState(entry.direct_output)
  const [timeout, setTimeoutMs] = useState(entry.timeout_ms ? String(entry.timeout_ms) : '')
  const [attempts, setAttempts] = useState(String(entry.retry?.max_attempts ?? 1))
  const [backoff, setBackoff] = useState(String(entry.retry?.backoff_ms ?? 0))
  const [retryOn, setRetryOn] = useState<string[]>(entry.retry?.retry_on ?? [])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const lines = examples.split('\n').map((line) => line.trim()).filter(Boolean)
  const tooMany = lines.length > MAX_EXAMPLES

  async function save(event: FormEvent) {
    event.preventDefault()
    if (tooMany) return
    setSaving(true); setError('')
    const attemptsNumber = Math.min(5, Math.max(1, Number(attempts) || 1))
    const customRetry = attemptsNumber > 1 || retryOn.length > 0 || Number(backoff) > 0
    try {
      await apiRequest(`/api/v1/registry/entries/${entry.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          intent_description: intent.trim(), intent_examples: lines, direct_enabled: direct, direct_output: direct && directOutput,
          timeout_ms: timeout.trim() ? Number(timeout) : null,
          retry: customRetry ? { max_attempts: attemptsNumber, backoff_ms: Math.max(0, Number(backoff) || 0), retry_on: retryOn } : null,
          row_version: entry.row_version,
        }),
      }, tenantId)
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" onClick={onClose} role="presentation">
      <form aria-label={entry.name} className="create-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void save(event)} role="dialog">
        <h2>{t('registry.dialog.title', { name: entry.name })}</h2>
        <p className="wizard-muted">{t('registry.dialog.source')}</p>
        <fieldset disabled={!canEdit || saving} style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 12 }}>
          <label className="kb-field"><span>{t('registry.dialog.intent')}</span><textarea maxLength={500} onChange={(event) => setIntent(event.target.value)} rows={3} value={intent} /><small>{t('registry.dialog.intentHint')}</small></label>
          <label className="kb-field"><span>{t('registry.dialog.examples')}</span><textarea onChange={(event) => setExamples(event.target.value)} rows={4} value={examples} /><small className={tooMany ? 'form-error' : undefined}>{t('registry.dialog.examplesHint', { max: MAX_EXAMPLES })}</small></label>
          <label className="governance-inherit"><input checked={direct} onChange={(event) => setDirect(event.target.checked)} type="checkbox" /><span><strong>{t('registry.dialog.direct')}</strong><small>{t('registry.dialog.directHint')}</small></span></label>
          <label className="governance-inherit"><input checked={directOutput} disabled={!direct} onChange={(event) => setDirectOutput(event.target.checked)} type="checkbox" /><span><strong>{t('registry.dialog.directOutput')}</strong><small>{t('registry.dialog.directOutputHint')}</small></span></label>
          <label className="kb-field"><span>{t('registry.dialog.timeout')}</span><input inputMode="numeric" onChange={(event) => setTimeoutMs(event.target.value.replace(/\D/g, ''))} placeholder={String(entry.policy.timeout_ms)} value={timeout} /><small>{t('registry.dialog.timeoutHint', { default: entry.policy.timeout_ms })}</small></label>
          <div style={{ display: 'flex', gap: 12 }}>
            <label className="kb-field"><span>{t('registry.dialog.attempts')}</span><input max={5} min={1} onChange={(event) => setAttempts(event.target.value)} type="number" value={attempts} /></label>
            <label className="kb-field"><span>{t('registry.dialog.backoff')}</span><input min={0} onChange={(event) => setBackoff(event.target.value)} type="number" value={backoff} /></label>
          </div>
          <div className="kb-field">
            <span>{t('registry.dialog.retryOn')}</span>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              {RETRY_ON.map((value) => (
                <label key={value} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <input checked={retryOn.includes(value)} onChange={(event) => setRetryOn(event.target.checked ? [...retryOn, value] : retryOn.filter((item) => item !== value))} type="checkbox" />
                  {t(`registry.dialog.retryOnItem.${value}`)}
                </label>
              ))}
            </div>
            <small>{t('registry.dialog.retryHint')}</small>
          </div>
        </fieldset>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <Link className="text-button" to={ownerPath(entry)}>{t('registry.dialog.open')}</Link>
          <button className="secondary-button" onClick={onClose} type="button">{t('registry.dialog.cancel')}</button>
          {canEdit && <button className="primary-button" disabled={saving || tooMany} type="submit">{saving ? t('registry.dialog.saving') : t('registry.dialog.save')}</button>}
        </div>
      </form>
    </div>
  )
}
