import { useCallback, useEffect, useRef, useState } from 'react'
import { Copy, Pencil, Plus, RefreshCw, Search, Trash2, Upload, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'

type Faq = {
  id: string
  question: string
  similar_questions: string[]
  answer: string
  status: 'draft' | 'published' | 'expired'
  expires_at: string | null
  hits_30d: number
  sync_status: string
  sync_error: string | null
  created_by: string | null
  row_version: number
}
type FaqPage = { items: Faq[]; total: number; counts: Record<string, number> }
type ImportResult = {
  dry_run: boolean
  recognized: number
  created: number
  errors: { line: number; message: string }[]
  preview: { line: number; question: string; answer: string; similar_questions: string[] }[]
}
type Draft = { id: string | null; question: string; similar: string[]; answer: string; status: 'draft' | 'published'; expires_at: string; row_version: number | null }

const STATUSES = ['all', 'published', 'draft', 'expired'] as const
const EMPTY: Draft = { id: null, question: '', similar: [], answer: '', status: 'draft', expires_at: '', row_version: null }

/** FAQ of a knowledge base (KNW-040–044): list, form, publish / unpublish, CSV import. */
export function FaqTab({ tenant, kbId, onChanged }: { tenant: TenantMembership; kbId: string; onChanged: () => void }) {
  const { t } = useTranslation('knowledge')
  const can = (action: string) => tenant.access_mode === 'full' && tenant.permissions.includes(`faq:${action}`)
  const base = `/api/v1/knowledge-bases/${kbId}/faqs`
  const [page, setPage] = useState<FaqPage | null>(null)
  const [status, setStatus] = useState<(typeof STATUSES)[number]>('all')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [tag, setTag] = useState('')
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState<Faq | null>(null)
  const [importing, setImporting] = useState<{ csv: string; result: ImportResult | null; publish: boolean; busy: boolean } | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: '100' })
    if (status !== 'all') params.set('status', status)
    if (search.trim()) params.set('q', search.trim())
    apiRequest<FaqPage>(`${base}?${params}`, {}, tenant.id)
      .then((result) => { setPage(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('faq.loadFailed')))
  }, [base, status, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])
  // Publishing reaches the engine through the Worker: keep refreshing while a sync is under way.
  const syncing = page?.items.some((faq) => faq.sync_status === 'pending' || faq.sync_status === 'processing') ?? false
  useEffect(() => {
    if (!syncing) return
    const timer = window.setInterval(() => { load(); onChanged() }, 3000)
    return () => window.clearInterval(timer)
  }, [syncing, load, onChanged])

  async function run(path: string, method: 'POST' | 'DELETE', message: string) {
    try {
      await apiRequest(`${base}/${path}`, { method }, tenant.id)
      setNotice(message)
      load()
      onChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  function edit(faq: Faq) {
    setFormError('')
    setTag('')
    setDraft({
      id: faq.id, question: faq.question, similar: faq.similar_questions, answer: faq.answer,
      status: faq.status === 'published' ? 'published' : 'draft', expires_at: faq.expires_at ?? '', row_version: faq.row_version,
    })
  }

  function addTag() {
    const value = tag.trim()
    if (!draft || !value) return
    if (!draft.similar.includes(value) && draft.similar.length < 10) setDraft({ ...draft, similar: [...draft.similar, value] })
    setTag('')
  }

  async function save() {
    if (!draft) return
    const question = draft.question.trim()
    const answer = draft.answer.trim()
    if (!question || question.length > 200) return setFormError(t('faq.questionInvalid'))
    if (!answer || answer.length > 2000) return setFormError(t('faq.answerInvalid'))
    setSaving(true)
    setFormError('')
    const body = {
      question, answer, similar_questions: draft.similar, status: draft.status,
      expires_at: draft.expires_at || null, row_version: draft.row_version,
    }
    try {
      await apiRequest(draft.id ? `${base}/${draft.id}` : base, { method: draft.id ? 'PUT' : 'POST', body: JSON.stringify(body) }, tenant.id)
      setNotice(t(draft.id ? 'faq.saved' : 'faq.created'))
      setDraft(null)
      load()
      onChanged()
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function sendCsv(csv: string, dryRun: boolean, publish: boolean) {
    setImporting((current) => (current ? { ...current, busy: true } : current))
    try {
      const result = await apiRequest<ImportResult>(
        `${base}/import?dry_run=${dryRun}&publish=${publish}`,
        { method: 'POST', body: csv, headers: { 'Content-Type': 'text/csv' } },
        tenant.id,
      )
      if (dryRun) {
        setImporting({ csv, result, publish, busy: false })
      } else {
        setImporting({ csv, result, publish, busy: false })
        setNotice(t('faq.imported', { count: result.created }))
        load()
        onChanged()
      }
    } catch (requestError) {
      setImporting(null)
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function chooseCsv(list: FileList | null) {
    const file = list?.[0]
    if (fileInput.current) fileInput.current.value = ''
    if (!file) return
    const buffer = await file.arrayBuffer()
    let text: string
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
    } catch {
      text = new TextDecoder('gb18030').decode(buffer)
    }
    setImporting({ csv: text, result: null, publish: false, busy: true })
    await sendCsv(text, true, false)
  }

  return (
    <div className="detail-card knowledge-documents">
      <div className="detail-card-head">
        <div><h2>{t('faq.title')}</h2><p className="usage-hint">{t('faq.subtitle')}</p></div>
        {can('edit') && (
          <div className="plans-actions">
            <button className="text-button" onClick={() => fileInput.current?.click()} type="button"><Upload size={14} /> {t('faq.import')}</button>
            <button className="primary-button" onClick={() => { setFormError(''); setTag(''); setDraft(EMPTY) }} type="button"><Plus size={14} /> {t('faq.new')}</button>
            <input accept=".csv,text/csv" hidden onChange={(event) => void chooseCsv(event.target.files)} ref={fileInput} type="file" />
          </div>
        )}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="knowledge-status-bar">
        {STATUSES.map((value) => (
          <button aria-pressed={status === value} className={status === value ? 'active' : ''} key={value} onClick={() => setStatus(value)} type="button">
            {t(`faq.statuses.${value}`)} <strong>{page?.counts[value] ?? 0}</strong>
          </button>
        ))}
        <label className="search-field"><Search size={14} /><input aria-label={t('faq.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('faq.search')} value={search} /></label>
      </div>
      {page && page.items.length === 0 && <p className="assistants-empty-filtered">{page.counts.all === 0 ? t('faq.empty0') : t('faq.noMatch')}</p>}
      {page && page.items.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('faq.colQuestion')}</th><th>{t('faq.colStatus')}</th><th>{t('faq.colExpires')}</th><th>{t('faq.colHits')}</th><th /></tr></thead>
            <tbody>
              {page.items.map((faq) => (
                <tr key={faq.id}>
                  <td>
                    <strong>{faq.question}</strong>
                    <small className="platform-tenant-slug">{faq.answer.length > 80 ? `${faq.answer.slice(0, 80)}…` : faq.answer}</small>
                    {faq.similar_questions.length > 0 && <small className="platform-tenant-slug">{t('faq.similarCount', { count: faq.similar_questions.length })}</small>}
                  </td>
                  <td>
                    <span className={`assistant-status knowledge-faq-${faq.status}`}>{t(`faq.statuses.${faq.status}`)}</span>
                    {faq.status === 'published' && faq.sync_status !== 'synced' && <small className={faq.sync_status === 'failed' || faq.sync_status === 'not_configured' ? 'form-error' : 'platform-tenant-slug'}>{t(`faq.sync.${faq.sync_status}`)}{faq.sync_error ? `：${faq.sync_error}` : ''}</small>}
                  </td>
                  <td>{faq.expires_at ?? '—'}</td>
                  <td>{faq.hits_30d}</td>
                  <td className="knowledge-doc-actions">
                    {can('publish') && faq.status === 'published' && faq.sync_status === 'failed' && <button aria-label={t('faq.retry')} className="icon-button" onClick={() => void run(`${faq.id}/retry`, 'POST', t('faq.retried'))} title={t('faq.retry')} type="button"><RefreshCw size={15} /></button>}
                    {can('publish') && faq.status === 'published' && <button className="text-button" onClick={() => void run(`${faq.id}/unpublish`, 'POST', t('faq.unpublished'))} type="button">{t('faq.unpublish')}</button>}
                    {can('publish') && faq.status !== 'published' && <button className="text-button" onClick={() => void run(`${faq.id}/publish`, 'POST', t('faq.published'))} type="button">{t('faq.publish')}</button>}
                    {can('edit') && <button aria-label={t('faq.edit')} className="icon-button" onClick={() => edit(faq)} title={t('faq.edit')} type="button"><Pencil size={15} /></button>}
                    {can('edit') && <button aria-label={t('faq.copy')} className="icon-button" onClick={() => void run(`${faq.id}/copy`, 'POST', t('faq.copied'))} title={t('faq.copy')} type="button"><Copy size={15} /></button>}
                    {can('delete') && <button aria-label={t('menuDelete')} className="icon-button" onClick={() => setDeleting(faq)} title={t('menuDelete')} type="button"><Trash2 size={15} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {draft && (
        <div className="modal-backdrop" onClick={() => !saving && setDraft(null)} role="presentation">
          <div aria-labelledby="faq-title" aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('cancel')} className="modal-close" onClick={() => setDraft(null)} type="button"><X size={18} /></button>
            <h2 id="faq-title">{t(draft.id ? 'faq.editTitle' : 'faq.new')}</h2>
            <label>{t('faq.question')}
              <input maxLength={200} onChange={(event) => setDraft({ ...draft, question: event.target.value })} value={draft.question} />
              <small>{draft.question.length}/200</small>
            </label>
            <label>{t('faq.similar')}
              <div className="knowledge-tags">
                {draft.similar.map((value) => (
                  <span className="assistant-status" key={value}>{value} <button aria-label={t('faq.removeTag', { name: value })} onClick={() => setDraft({ ...draft, similar: draft.similar.filter((item) => item !== value) })} type="button"><X size={12} /></button></span>
                ))}
              </div>
              <input
                onBlur={addTag}
                onChange={(event) => setTag(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addTag() } }}
                placeholder={t('faq.similarPlaceholder')}
                value={tag}
              />
            </label>
            <label>{t('faq.answer')}
              <textarea maxLength={2000} onChange={(event) => setDraft({ ...draft, answer: event.target.value })} rows={6} value={draft.answer} />
              <small>{draft.answer.length}/2000</small>
            </label>
            <label>{t('faq.expires')}
              <input onChange={(event) => setDraft({ ...draft, expires_at: event.target.value })} type="date" value={draft.expires_at} />
              <small>{t('faq.expiresHint')}</small>
            </label>
            <label>{t('faq.colStatus')}
              <select disabled={!can('publish')} onChange={(event) => setDraft({ ...draft, status: event.target.value as 'draft' | 'published' })} value={draft.status}>
                <option value="draft">{t('faq.statuses.draft')}</option>
                <option value="published">{t('faq.statuses.published')}</option>
              </select>
            </label>
            {formError && <p className="form-error" role="alert">{formError}</p>}
            <div className="plans-actions">
              <button className="text-button" disabled={saving} onClick={() => setDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving} onClick={() => void save()} type="button">{saving ? t('saving') : t('save')}</button>
            </div>
          </div>
        </div>
      )}

      {importing && (
        <div className="modal-backdrop" role="presentation">
          <div aria-labelledby="faq-import-title" aria-modal="true" className="create-modal knowledge-preview" role="dialog">
            <button aria-label={t('cancel')} className="modal-close" onClick={() => setImporting(null)} type="button"><X size={18} /></button>
            <h2 id="faq-import-title">{t('faq.import')}</h2>
            <p className="usage-hint">{t('faq.importHint')}</p>
            {importing.busy && <p role="status">{t('faq.importing')}</p>}
            {importing.result && (
              <>
                <p role="status">
                  {importing.result.dry_run
                    ? t('faq.importPreview', { count: importing.result.recognized, errors: importing.result.errors.length })
                    : t('faq.imported', { count: importing.result.created })}
                </p>
                {importing.result.errors.length > 0 && (
                  <ul className="form-error" aria-label={t('faq.importErrors')}>
                    {importing.result.errors.map((item) => <li key={`${item.line}-${item.message}`}>{t('faq.importLine', { line: item.line })}：{item.message}</li>)}
                  </ul>
                )}
                {importing.result.dry_run && importing.result.preview.length > 0 && (
                  <ol>{importing.result.preview.map((row) => <li key={row.line}><strong>{row.question}</strong> — {row.answer}</li>)}</ol>
                )}
                {importing.result.dry_run && can('publish') && (
                  <label className="checkbox-row"><input checked={importing.publish} onChange={(event) => setImporting({ ...importing, publish: event.target.checked })} type="checkbox" /> {t('faq.importPublish')}</label>
                )}
              </>
            )}
            <div className="plans-actions">
              <button className="text-button" onClick={() => setImporting(null)} type="button">{importing.result?.dry_run ? t('cancel') : t('faq.close')}</button>
              {importing.result?.dry_run && (
                <button className="primary-button" disabled={importing.busy || importing.result.recognized === 0} onClick={() => void sendCsv(importing.csv, false, importing.publish)} type="button">{t('faq.importConfirm', { count: importing.result.recognized })}</button>
              )}
            </div>
          </div>
        </div>
      )}

      {deleting && <ConfirmDialog body={t('faq.deleteBody', { name: deleting.question })} danger onCancel={() => setDeleting(null)} onConfirm={async () => { const target = deleting; setDeleting(null); await run(target.id, 'DELETE', t('faq.deleted')) }} title={t('faq.deleteTitle')} />}
    </div>
  )
}
