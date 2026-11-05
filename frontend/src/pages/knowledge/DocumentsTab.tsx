import { useCallback, useEffect, useRef, useState } from 'react'
import { Download, ExternalLink, Eye, RefreshCw, Search, Trash2, Upload, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ApiRequestError, apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantMembership } from '../../types'

type Doc = {
  id: string
  name: string
  format: 'pdf' | 'docx' | 'html' | 'markdown' | 'txt' | 'url'
  source_url: string | null
  size_bytes: number
  chunks: number
  owner_name: string | null
  status: 'processing' | 'indexed' | 'failed'
  error_message: string | null
  created_at: string
  can_download: boolean
}
type DocPage = { items: Doc[]; total: number; counts: Record<string, number> }

const MAX_BYTES = 50 * 1024 * 1024
const MAX_FILES = 20
const EXTENSIONS = ['pdf', 'docx', 'html', 'htm', 'md', 'markdown', 'txt']
const STATUSES = ['all', 'indexed', 'processing', 'failed'] as const

function formatSize(bytes: number) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

/** Documents of a knowledge base (KNW-030): upload, URL import, status bar, chunk preview, re-index, delete. */
export function DocumentsTab({ tenant, kbId, onChanged }: { tenant: TenantMembership; kbId: string; onChanged: () => void }) {
  const { t } = useTranslation('knowledge')
  const canEdit = tenant.access_mode === 'full' && tenant.permissions.includes('knowledge:edit')
  const base = `/api/v1/knowledge-bases/${kbId}/documents`
  const [page, setPage] = useState<DocPage | null>(null)
  const [status, setStatus] = useState<(typeof STATUSES)[number]>('all')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [uploading, setUploading] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [urls, setUrls] = useState('')
  const [conflicts, setConflicts] = useState<File[]>([])
  const [deleting, setDeleting] = useState<Doc | null>(null)
  const [preview, setPreview] = useState<{ doc: Doc; items: string[]; total: number; page: number } | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: '100' })
    if (status !== 'all') params.set('status', status)
    if (search.trim()) params.set('q', search.trim())
    apiRequest<DocPage>(`${base}?${params}`, {}, tenant.id)
      .then((result) => { setPage(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('documents.loadFailed')))
  }, [base, status, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])
  // Parsing happens in the engine: keep refreshing while anything is still processing.
  const anyProcessing = (page?.counts.processing ?? 0) > 0
  useEffect(() => {
    if (!anyProcessing) return
    const timer = window.setInterval(() => { load(); onChanged() }, 3000)
    return () => window.clearInterval(timer)
  }, [anyProcessing, load, onChanged])

  function validate(files: File[]): File[] {
    const accepted: File[] = []
    for (const file of files.slice(0, MAX_FILES)) {
      const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
      if (!EXTENSIONS.includes(extension)) { setError(t('documents.unsupported', { name: file.name })); continue }
      if (file.size > MAX_BYTES) { setError(t('documents.tooLarge', { name: file.name })); continue }
      if (file.size === 0) { setError(t('documents.empty', { name: file.name })); continue }
      accepted.push(file)
    }
    if (files.length > MAX_FILES) setError(t('documents.tooMany', { count: MAX_FILES }))
    return accepted
  }

  async function sendFiles(files: File[], onConflict: 'ask' | 'overwrite' | 'keep_both') {
    setUploading(true)
    const duplicates: File[] = []
    let sent = 0
    for (const file of files) {  // one request per file: the file is the raw request body
      try {
        await apiRequest(`${base}?filename=${encodeURIComponent(file.name)}&on_conflict=${onConflict}`, {
          method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' },
        }, tenant.id)
        sent += 1
      } catch (requestError) {
        if (requestError instanceof ApiRequestError && requestError.code === 'knowledge.duplicate_name') duplicates.push(file)
        else setError(`${file.name}：${requestError instanceof Error ? requestError.message : t('documents.uploadFailed')}`)
      }
    }
    setUploading(false)
    if (sent) setNotice(t('documents.uploaded', { count: sent }))
    setConflicts(duplicates)
    setDialog(false)
    load()
    onChanged()
  }

  async function chooseFiles(list: FileList | null) {
    if (!list || list.length === 0) return
    setError('')
    const accepted = validate(Array.from(list))
    if (fileInput.current) fileInput.current.value = ''
    if (accepted.length) await sendFiles(accepted, 'ask')
  }

  async function importUrls() {
    const list = urls.split('\n').map((line) => line.trim()).filter(Boolean)
    if (!list.length) return
    setUploading(true)
    setError('')
    try {
      const created = await apiRequest<Doc[]>(`${base}/urls`, { method: 'POST', body: JSON.stringify({ urls: list }) }, tenant.id)
      setNotice(t('documents.uploaded', { count: created.length }))
      setUrls('')
      setDialog(false)
      load()
      onChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('documents.uploadFailed'))
    } finally {
      setUploading(false)
    }
  }

  async function action(path: string, method: 'POST' | 'DELETE', message: string) {
    try {
      await apiRequest(`${base}/${path}`, { method }, tenant.id)
      setNotice(message)
      load()
      onChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function download(doc: Doc) {
    if (doc.format === 'url') {
      if (doc.source_url) window.open(doc.source_url, '_blank', 'noopener,noreferrer')
      return
    }
    try {
      const response = await fetch(`${base}/${doc.id}/download`, { credentials: 'same-origin', headers: { 'X-Tenant-ID': tenant.id } })
      if (!response.ok) throw new Error(t('documents.downloadFailed'))
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = doc.name
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('documents.downloadFailed'))
    }
  }

  async function openPreview(doc: Doc, pageNumber = 1) {
    try {
      const result = await apiRequest<{ items: string[]; total: number }>(`${base}/${doc.id}/chunks?page=${pageNumber}&size=10`, {}, tenant.id)
      setPreview({ doc, items: result.items, total: result.total, page: pageNumber })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  return (
    <div className="detail-card knowledge-documents">
      <div className="detail-card-head">
        <div><h2>{t('documents.title')}</h2><p className="usage-hint">{t('documents.subtitle')}</p></div>
        {canEdit && <button className="primary-button" onClick={() => setDialog(true)} type="button"><Upload size={14} /> {t('documents.upload')}</button>}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="knowledge-status-bar">
        {STATUSES.map((value) => (
          <button aria-pressed={status === value} className={status === value ? 'active' : ''} key={value} onClick={() => setStatus(value)} type="button">
            {t(`documents.statuses.${value}`)} <strong>{page?.counts[value] ?? 0}</strong>
          </button>
        ))}
        <label className="search-field"><Search size={14} /><input aria-label={t('documents.search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('documents.search')} value={search} /></label>
      </div>
      {page && page.items.length === 0 && <p className="assistants-empty-filtered">{page.counts.all === 0 ? t('documents.empty0') : t('documents.noMatch')}</p>}
      {page && page.items.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('documents.colName')}</th><th>{t('documents.colFormat')}</th><th>{t('documents.colChunks')}</th><th>{t('documents.colOwner')}</th><th>{t('documents.colStatus')}</th><th /></tr></thead>
            <tbody>
              {page.items.map((doc) => (
                <tr key={doc.id}>
                  <td><strong>{doc.name}</strong><small className="platform-tenant-slug">{doc.format === 'url' ? doc.source_url : formatSize(doc.size_bytes)} · {formatRelativeTime(doc.created_at)}</small></td>
                  <td>{t(`documents.formats.${doc.format}`)}</td>
                  <td>{doc.status === 'indexed' ? doc.chunks : '—'}</td>
                  <td>{doc.owner_name ?? '—'}</td>
                  <td>
                    <span className={`assistant-status knowledge-doc-${doc.status}`}>{t(`documents.statuses.${doc.status}`)}</span>
                    {doc.status === 'failed' && doc.error_message && <small className="form-error">{doc.error_message}</small>}
                  </td>
                  <td className="knowledge-doc-actions">
                    {doc.status === 'indexed' && <button aria-label={t('documents.view')} className="icon-button" onClick={() => void openPreview(doc)} title={t('documents.view')} type="button"><Eye size={15} /></button>}
                    <button aria-label={doc.format === 'url' ? t('documents.openLink') : t('documents.download')} className="icon-button" onClick={() => void download(doc)} title={doc.format === 'url' ? t('documents.openLink') : t('documents.download')} type="button">{doc.format === 'url' ? <ExternalLink size={15} /> : <Download size={15} />}</button>
                    {canEdit && doc.status !== 'processing' && <button aria-label={t('documents.reindex')} className="icon-button" onClick={() => void action(`${doc.id}/reindex`, 'POST', t('documents.reindexQueued'))} title={t('documents.reindex')} type="button"><RefreshCw size={15} /></button>}
                    {canEdit && <button aria-label={t('menuDelete')} className="icon-button" onClick={() => setDeleting(doc)} title={t('menuDelete')} type="button"><Trash2 size={15} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {dialog && (
        <div className="modal-backdrop" onClick={() => !uploading && setDialog(false)} role="presentation">
          <div aria-labelledby="upload-title" aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('cancel')} className="modal-close" onClick={() => setDialog(false)} type="button"><X size={18} /></button>
            <h2 id="upload-title">{t('documents.upload')}</h2>
            <p className="usage-hint">{t('documents.uploadHint', { count: MAX_FILES })}</p>
            <input accept=".pdf,.docx,.html,.htm,.md,.markdown,.txt" disabled={uploading} multiple onChange={(event) => void chooseFiles(event.target.files)} ref={fileInput} type="file" />
            <label>{t('documents.urlsLabel')}
              <textarea disabled={uploading} onChange={(event) => setUrls(event.target.value)} placeholder="https://example.com/help" rows={4} value={urls} />
              <small>{t('documents.urlsHint')}</small>
            </label>
            {uploading && <p role="status">{t('documents.uploading')}</p>}
            <button className="primary-button" disabled={uploading || !urls.trim()} onClick={() => void importUrls()} type="button">{t('documents.importUrls')}</button>
          </div>
        </div>
      )}

      {conflicts.length > 0 && (
        <div className="modal-backdrop" role="presentation">
          <div aria-labelledby="conflict-title" aria-modal="true" className="create-modal" role="dialog">
            <h2 id="conflict-title">{t('documents.conflictTitle')}</h2>
            <p>{t('documents.conflictBody', { names: conflicts.map((file) => file.name).join('、') })}</p>
            <div className="plans-actions">
              <button className="text-button" onClick={() => setConflicts([])} type="button">{t('documents.skip')}</button>
              <button className="text-button" onClick={() => void sendFiles(conflicts, 'keep_both')} type="button">{t('documents.keepBoth')}</button>
              <button className="primary-button" onClick={() => void sendFiles(conflicts, 'overwrite')} type="button">{t('documents.overwrite')}</button>
            </div>
          </div>
        </div>
      )}

      {preview && (
        <div className="modal-backdrop" onClick={() => setPreview(null)} role="presentation">
          <div aria-labelledby="chunks-title" aria-modal="true" className="create-modal knowledge-preview" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('cancel')} className="modal-close" onClick={() => setPreview(null)} type="button"><X size={18} /></button>
            <h2 id="chunks-title">{preview.doc.name}</h2>
            <p className="usage-hint">{t('documents.chunkTotal', { count: preview.total })}</p>
            <ol start={(preview.page - 1) * 10 + 1}>{preview.items.map((text, index) => <li key={index}><pre>{text}</pre></li>)}</ol>
            <div className="plans-actions">
              <button className="text-button" disabled={preview.page <= 1} onClick={() => void openPreview(preview.doc, preview.page - 1)} type="button">{t('documents.prev')}</button>
              <button className="text-button" disabled={preview.page * 10 >= preview.total} onClick={() => void openPreview(preview.doc, preview.page + 1)} type="button">{t('documents.next')}</button>
            </div>
          </div>
        </div>
      )}

      {deleting && <ConfirmDialog body={t('documents.deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={async () => { const target = deleting; setDeleting(null); await action(target.id, 'DELETE', t('documents.deleted')) }} title={t('documents.deleteTitle')} />}
    </div>
  )
}
