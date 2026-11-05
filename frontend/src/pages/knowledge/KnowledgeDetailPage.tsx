import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { ApiRequestError, apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import { DocumentsTab } from './DocumentsTab'
import type { KnowledgeBase } from './types'

const EMBEDDINGS = ['bge-m3', 'text-embedding-3-large', 'cohere-embed-v3'] as const
const CHUNKINGS = ['semantic', 'fixed_512', 'by_heading'] as const

/** Knowledge base detail (KNW-020–022) and its settings view (KNW-032). Documents and FAQ tabs arrive with T-031 / T-032. */
export function KnowledgeDetailPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('knowledge')
  const { kbId } = useParams()
  const location = useLocation()
  const [search] = useSearchParams()
  const settingsView = search.get('view') === 'settings'
  const canEdit = tenant.access_mode === 'full' && tenant.permissions.includes('knowledge:edit')
  const [kb, setKb] = useState<KnowledgeBase | null>(null)
  const [form, setForm] = useState<KnowledgeBase | null>(null)
  const [tab, setTab] = useState<'documents' | 'faq'>('documents')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState((location.state as { notice?: string } | null)?.notice ?? '')
  const [saving, setSaving] = useState(false)
  const [confirmReindex, setConfirmReindex] = useState(false)

  const load = useCallback(() => {
    apiRequest<KnowledgeBase>(`/api/v1/knowledge-bases/${kbId}`, {}, tenant.id)
      .then((result) => { setKb(result); setForm(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [kbId, tenant.id, t])
  useEffect(() => { load() }, [load])
  // Dataset work runs in the Worker, so follow the sync state until it settles.
  useEffect(() => {
    if (!kb || (kb.sync_status !== 'pending' && !kb.reindexing)) return
    const timer = window.setTimeout(load, 3000)
    return () => window.clearTimeout(timer)
  }, [kb, load])

  if (error && !kb) return <section className="knowledge-page"><Link className="text-button" to="/knowledge"><ArrowLeft size={14} /> {t('backToAll')}</Link><p className="form-error" role="alert">{error}</p></section>
  if (!kb || !form) return <section className="knowledge-page"><p>{t('loading')}</p></section>

  const isFaq = kb.kb_type === 'faq'
  const indexChanged = !isFaq && form.kb_type === 'standard' && (form.embedding_model !== kb.embedding_model || form.chunking !== kb.chunking)
  const set = <K extends keyof KnowledgeBase>(key: K, value: KnowledgeBase[K]) => setForm({ ...form, [key]: value })

  async function save(event: FormEvent, reindexConfirmed = false) {
    event.preventDefault()
    if (!form || !kb) return
    if (indexChanged && kb.docs_count > 0 && !reindexConfirmed) {
      setConfirmReindex(true)
      return
    }
    setConfirmReindex(false)
    setSaving(true)
    setError('')
    try {
      await apiRequest(`/api/v1/knowledge-bases/${kb.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: form.name, description: form.description, language: form.language, kb_type: form.kb_type,
          embedding_model: form.embedding_model, chunking: form.chunking, retrieval_threshold: form.retrieval_threshold,
          row_version: kb.row_version, confirm_reindex: reindexConfirmed,
        }),
      }, tenant.id)
      setNotice(form.kb_type !== kb.kb_type ? t(form.kb_type === 'faq' ? 'switchedToFaq' : 'switchedToStandard') : t('settingsSaved'))
      load()
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'conflict.stale') load()
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function retrySync() {
    try {
      await apiRequest(`/api/v1/knowledge-bases/${kb?.id}/retry-sync`, { method: 'POST' }, tenant.id)
      setNotice(t('retryQueued'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  return (
    <section className="knowledge-page">
      <Link className="text-button" to="/knowledge"><ArrowLeft size={14} /> {t('backToAll')}</Link>
      <div className="page-heading">
        <div>
          <small>{isFaq ? 'FAQ KNOWLEDGE BASE' : 'KNOWLEDGE BASE'}</small>
          <h1>{kb.name}</h1>
          <p>{kb.description || t('detailDefaultDescription')}</p>
        </div>
        <div className="knowledge-badges">
          <span className="assistant-status">{t(`type.${kb.kb_type}`)}</span>
          <span className="assistant-status">{t(`engine.${kb.engine}`)}</span>
          <span className={`assistant-status knowledge-status-${kb.status}`}>{t(`status.${kb.status}`)}</span>
          {settingsView && <span className="assistant-status">{t('settingsBadge')}</span>}
          {!settingsView && <Link className="text-button" to={`/knowledge/${kb.id}?view=settings`}>{t('menuSettings')}</Link>}
        </div>
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {kb.sync_status !== 'synced' && (
        <div className={`knowledge-sync-banner ${kb.sync_status}`} role="status">
          <span>{t(`syncLong.${kb.sync_status}`)}{kb.sync_error ? `：${kb.sync_error}` : ''}</span>
          {canEdit && (kb.sync_status === 'failed' || kb.sync_status === 'not_configured') && <button className="text-button" onClick={() => void retrySync()} type="button">{t('retrySync')}</button>}
        </div>
      )}

      {!settingsView && (
        <>
          <div className="knowledge-tabs" role="tablist">
            {!isFaq && <button aria-selected={tab === 'documents'} onClick={() => setTab('documents')} role="tab" type="button">{t('tabDocuments', { count: kb.docs_count })}</button>}
            <button aria-selected={isFaq || tab === 'faq'} onClick={() => setTab('faq')} role="tab" type="button">{t('tabFaq', { count: kb.faq_count })}</button>
          </div>
          {!isFaq && tab === 'documents'
            ? <DocumentsTab kbId={kb.id} onChanged={load} tenant={tenant} />
            : <div className="detail-card"><p className="usage-hint">{t('faqSoon')}</p></div>}
        </>
      )}

      {settingsView && (
        <form className="detail-card knowledge-form" onSubmit={(event) => void save(event)}>
          <h2>{t('settingsTitle')}</h2>
          <p className="usage-hint">{t('settingsSubtitle')}</p>
          <label>{t('name')}<input disabled={!canEdit} maxLength={50} onChange={(event) => set('name', event.target.value)} required value={form.name} /></label>
          <label>{t('description')}<textarea disabled={!canEdit} maxLength={500} onChange={(event) => set('description', event.target.value)} rows={3} value={form.description} /></label>
          <label>{t('languageLabel')}
            <select disabled={!canEdit} onChange={(event) => set('language', event.target.value as KnowledgeBase['language'])} value={form.language}>
              {(['multilingual', 'zh-CN', 'en'] as const).map((value) => <option key={value} value={value}>{t(`language.${value}`)}</option>)}
            </select>
          </label>
          <label>{t('typeLabel')}
            <select disabled={!canEdit} onChange={(event) => set('kb_type', event.target.value as KnowledgeBase['kb_type'])} value={form.kb_type}>
              <option value="standard">{t('type.standard')}</option>
              <option disabled={kb.docs_count > 0} value="faq">{t('type.faq')}</option>
            </select>
            <small>{kb.docs_count > 0 ? t('typeBlocked', { count: kb.docs_count }) : t('typeSwitchHint')}</small>
          </label>
          {form.kb_type === 'standard' && (
            <>
              <label>{t('embedding')}
                <select disabled={!canEdit} onChange={(event) => set('embedding_model', event.target.value as KnowledgeBase['embedding_model'])} value={form.embedding_model}>
                  {EMBEDDINGS.map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
              <label>{t('chunkingLabel')}
                <select disabled={!canEdit} onChange={(event) => set('chunking', event.target.value as KnowledgeBase['chunking'])} value={form.chunking}>
                  {CHUNKINGS.map((value) => <option key={value} value={value}>{t(`chunking.${value}`)}</option>)}
                </select>
              </label>
              {indexChanged && <p className="usage-hint" role="note">{t('reindexNote')}</p>}
            </>
          )}
          <label>{t('threshold')}
            <span className="knowledge-threshold">
              <input disabled={!canEdit} max={1} min={0} onChange={(event) => set('retrieval_threshold', event.target.value)} step={0.01} type="range" value={form.retrieval_threshold} aria-label={t('threshold')} />
              <input disabled={!canEdit} max={1} min={0} onChange={(event) => set('retrieval_threshold', event.target.value)} step={0.01} type="number" value={form.retrieval_threshold} />
            </span>
            <small>{t('thresholdHint')}</small>
          </label>
          {canEdit
            ? <button className="primary-button" disabled={saving || !form.name.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
            : <small>{t('readOnly')}</small>}
          {confirmReindex && (
            <div className="modal-backdrop" onClick={() => setConfirmReindex(false)} role="presentation">
              <div aria-labelledby="reindex-title" aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
                <h2 id="reindex-title">{t('reindexTitle')}</h2>
                <p>{t('reindexBody', { count: kb.docs_count })}</p>
                <div className="plans-actions">
                  <button className="text-button" onClick={() => setConfirmReindex(false)} type="button">{t('cancel')}</button>
                  <button className="primary-button" onClick={(event) => void save(event, true)} type="button">{t('reindexConfirm')}</button>
                </div>
              </div>
            </div>
          )}
        </form>
      )}
    </section>
  )
}
