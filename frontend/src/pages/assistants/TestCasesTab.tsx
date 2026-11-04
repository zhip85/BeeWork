import { useCallback, useEffect, useState } from 'react'
import { Copy, Pencil, Play, Plus, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantMembership } from '../../types'
import { TestCaseDialog } from './TestCaseDialog'
import { CASE_TYPES } from './testCaseTypes'
import type { TestCase } from './TestCaseDialog'
import type { Assistant } from './types'

type RunItem = { case_id: string; name: string; status: string; reason: string }
type TestRun = {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  target_label: string
  total: number
  completed: number
  passed: number
  failed: number
  results: RunItem[]
  error: string | null
}
type Listing = { items: TestCase[]; total: number; passed: number; failed: number; not_run: number; active_run: TestRun | null; limit: number }

const POLL_MS = 1500
const ACTIVE = ['queued', 'running']

/** Test cases (AST-071): CRUD, run one or all by the worker, results with the judge's reason. */
export function TestCasesTab({ assistant, tenant, versions }: { assistant: Assistant; tenant: TenantMembership; versions: { id: string; version: string; is_live: boolean }[] }) {
  const { t } = useTranslation('testing')
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && tenant.permissions.includes('assistants:edit') && assistant.can_edit
  const canRun = writable && tenant.permissions.includes('assistants:test')
  const base = `/api/v1/assistants/${assistant.id}`

  const [data, setData] = useState<Listing | null>(null)
  const [run, setRun] = useState<TestRun | null>(null)
  const [status, setStatus] = useState('')
  const [type, setType] = useState('')
  const [search, setSearch] = useState('')
  const [target, setTarget] = useState('draft')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState<Partial<TestCase> | null>(null)
  const [viewing, setViewing] = useState<TestCase | null>(null)
  const [deleting, setDeleting] = useState<TestCase | null>(null)
  const [reload, setReload] = useState(0)

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  useEffect(() => {
    let active = true
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (type) params.set('type', type)
    if (search.trim()) params.set('q', search.trim())
    const timer = window.setTimeout(() => {
      apiRequest<Listing>(`${base}/test-cases?${params}`, {}, tenantId)
        .then((result) => {
          if (!active) return
          setData(result)
          setRun((current) => current ?? result.active_run)
          setError('')
        })
        .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    }, 200)
    return () => { active = false; window.clearTimeout(timer) }
  }, [base, tenantId, status, type, search, reload, t])

  // Progress: poll the batch while it is queued or running, refreshing the table as cases finish.
  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return
    let active = true
    const timer = window.setInterval(() => {
      apiRequest<TestRun>(`${base}/test-runs/${run.id}`, {}, tenantId)
        .then((result) => { if (active) { setRun(result); refresh() } })
        .catch(() => undefined)
    }, POLL_MS)
    return () => { active = false; window.clearInterval(timer) }
  }, [run, base, tenantId, refresh])

  async function start(caseIds?: string[]) {
    setError('')
    setNotice('')
    try {
      const result = await apiRequest<{ run: TestRun }>(`${base}/test-runs`, {
        method: 'POST',
        body: JSON.stringify({ case_ids: caseIds, version_id: target === 'draft' ? undefined : target }),
      }, tenantId)
      setRun(result.run)
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function cancel() {
    if (!run) return
    try { setRun(await apiRequest<TestRun>(`${base}/test-runs/${run.id}/cancel`, { method: 'POST' }, tenantId)) } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function duplicate(item: TestCase) {
    try {
      await apiRequest(`${base}/test-cases/${item.id}/copy`, { method: 'POST' }, tenantId)
      setNotice(t('copied'))
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function remove(item: TestCase) {
    await apiRequest(`${base}/test-cases/${item.id}`, { method: 'DELETE' }, tenantId)
    setDeleting(null)
    setNotice(t('deleted'))
    refresh()
  }

  const running = run !== null && ACTIVE.includes(run.status)
  const progress = run && run.total > 0 ? Math.round((run.completed / run.total) * 100) : 0

  return (
    <div className="detail-card test-cases">
      <header className="detail-card-head">
        <h2>{t('title')} {data ? `· ${data.total}` : ''}</h2>
        <div className="detail-actions">
          {canRun && (
            <>
              <select aria-label={t('runTarget')} disabled={running} onChange={(event) => setTarget(event.target.value)} value={target}>
                <option value="draft">{t('console.draft')}</option>
                {versions.map((version) => <option key={version.id} value={version.id}>v{version.version}{version.is_live ? ` · ${t('console.live')}` : ''}</option>)}
              </select>
              <button className="secondary-button" disabled={running || !data || data.total === 0} onClick={() => void start()} type="button"><Play size={14} /> {t('runAll')}</button>
            </>
          )}
          {canEdit && <button className="primary-button" disabled={!data || data.total >= data.limit} onClick={() => setEditing({})} type="button"><Plus size={14} /> {t('new')}</button>}
        </div>
      </header>

      {data && <p className="wizard-muted">{t('summary', { passed: data.passed, failed: data.failed, notRun: data.not_run })}</p>}
      {run && (
        <div className={`test-run-progress ${run.status}`} role="status">
          <div className="bar"><span style={{ width: `${progress}%` }} /></div>
          <small>
            {running
              ? t('progress.running', { completed: run.completed, total: run.total, target: run.target_label })
              : run.status === 'succeeded' ? t('progress.done', { passed: run.passed, failed: run.failed, total: run.total, target: run.target_label })
              : run.status === 'cancelled' ? t('progress.cancelled', { completed: run.completed, total: run.total })
              : t('progress.failed', { error: run.error ?? '' })}
          </small>
          {running && canRun && <button className="text-button" onClick={() => void cancel()} type="button">{t('cancelRun')}</button>}
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="assistants-toolbar">
        <input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} />
        <select aria-label={t('fields.status')} onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">{t('filters.allStatuses')}</option>
          {['passed', 'failed', 'not_run'].map((key) => <option key={key} value={key}>{t(`status.${key}`)}</option>)}
        </select>
        <select aria-label={t('fields.type')} onChange={(event) => setType(event.target.value)} value={type}>
          <option value="">{t('filters.allTypes')}</option>
          {CASE_TYPES.map((key) => <option key={key} value={key}>{t(`types.${key}`)}</option>)}
        </select>
      </div>

      {data && data.items.length === 0 && <p className="assistants-empty-filtered">{data.total === 0 ? t('empty') : t('emptyFiltered')}</p>}
      {data && data.items.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('fields.name')}</th><th>{t('fields.type')}</th><th>{t('fields.status')}</th><th>{t('lastRun')}</th><th /></tr></thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id}>
                  <td><button className="text-button" onClick={() => setViewing(item)} type="button"><strong>{item.name}</strong></button><small className="platform-tenant-slug">{item.input.slice(0, 60)}</small></td>
                  <td>{t(`types.${item.type}`)}</td>
                  <td>
                    {item.run_state !== 'idle'
                      ? <span className="approval-status">{t(`runState.${item.run_state}`)}</span>
                      : <span className={`approval-status ${item.status === 'passed' ? 'approved' : item.status === 'failed' ? 'rejected' : 'withdrawn'}`}>{t(`status.${item.status}`)}</span>}
                  </td>
                  <td>{item.last_run_at ? `${item.last_target ?? ''} · ${formatRelativeTime(item.last_run_at)}` : '—'}</td>
                  <td className="row-actions">
                    {canRun && <button aria-label={t('run')} className="icon-button" disabled={running} onClick={() => void start([item.id])} title={t('run')} type="button"><Play size={14} /></button>}
                    {canEdit && <button aria-label={t('edit')} className="icon-button" disabled={item.run_state !== 'idle'} onClick={() => setEditing(item)} title={t('edit')} type="button"><Pencil size={14} /></button>}
                    {canEdit && <button aria-label={t('copy')} className="icon-button" onClick={() => void duplicate(item)} title={t('copy')} type="button"><Copy size={14} /></button>}
                    {canEdit && <button aria-label={t('delete')} className="icon-button" onClick={() => setDeleting(item)} title={t('delete')} type="button"><Trash2 size={14} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && <TestCaseDialog assistantId={assistant.id} initial={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice(t('saved')); refresh() }} tenantId={tenantId} />}
      {deleting && <ConfirmDialog body={t('confirmDelete', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('delete')} />}
      {viewing && (
        <div className="modal-backdrop" onClick={() => setViewing(null)} role="presentation">
          <aside aria-modal="true" className="conversation-drawer" onClick={(event) => event.stopPropagation()} role="dialog">
            <header><h2>{viewing.name}</h2><button aria-label={t('close')} className="modal-close" onClick={() => setViewing(null)} type="button"><X size={18} /></button></header>
            <h3>{t('fields.input')}</h3><pre className="version-lines">{viewing.input}</pre>
            <h3>{t('fields.expected')}</h3><pre className="version-lines">{viewing.expected}</pre>
            <h3>{t('result.title')}</h3>
            {viewing.status === 'not_run' && !viewing.last_reason ? <p className="wizard-muted">{t('result.none')}</p> : (
              <>
                <p><span className={`approval-status ${viewing.status === 'passed' ? 'approved' : 'rejected'}`}>{t(`status.${viewing.status}`)}</span> {viewing.last_target && <small>{viewing.last_target}</small>}</p>
                <p><strong>{t('result.reason')}</strong> {viewing.last_reason}</p>
                {viewing.last_output && <><h3>{t('result.actual')}</h3><pre className="version-lines">{viewing.last_output}</pre></>}
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  )
}
