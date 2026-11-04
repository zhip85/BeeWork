import { useCallback, useEffect, useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { formatRelativeTime } from '../../i18n/format'
import type { TenantMembership } from '../../types'
import { ChangeList } from '../assistants/VersionDiff'
import type { VersionChange } from '../assistants/VersionDiff'

type Check = { label: string; level?: 'ok' | 'warn' }
export type ApprovalItem = {
  id: string
  title: string
  type: string
  object_type: string
  organization_name: string
  requester_name: string
  status: string
  apply_status: string
  current_level: number
  level_count: number
  approver_names: string[]
  checks: Check[]
  submitted_at: string
  decided_at: string | null
  can_decide: boolean
  can_withdraw: boolean
  can_retry: boolean
}
type Level = { level: number; approver_role: string; approvers: string[]; status: string; decided_by: string | null; comment: string; decided_at: string | null }
type Detail = ApprovalItem & {
  description: string
  change_diff: VersionChange[]
  levels: Level[]
  comments: { id: string; author_name: string; body: string; created_at: string }[]
  apply_error: string | null
}
type Summary = { pending_mine: number; submitted: number; completed_this_month: number; avg_duration_seconds: number | null }
type Scope = 'pending_mine' | 'submitted' | 'all'

const TYPES = ['publish', 'capability_access', 'data_export', 'rule_change'] as const
const STATUSES = ['pending', 'approved', 'rejected', 'withdrawn', 'expired'] as const

function duration(seconds: number | null) {
  if (seconds === null) return '—'
  const minutes = Math.round(seconds / 60)
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`
}

export function ApprovalsPage({ tenant, onChanged }: { tenant: TenantMembership; onChanged: () => void }) {
  const { t } = useTranslation('approvals')
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'

  const [scope, setScope] = useState<Scope>('pending_mine')
  const [type, setType] = useState('')
  const [status, setStatus] = useState('')
  const [sort, setSort] = useState('oldest')
  const [search, setSearch] = useState('')
  const [items, setItems] = useState<ApprovalItem[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const [params, setParams] = useSearchParams()
  const openId = params.get('id')
  const setOpenId = (id: string | null) => setParams(id ? { id } : {}, { replace: true })

  const query = useMemo(() => {
    const params = new URLSearchParams({ scope, sort, limit: '100' })
    if (type) params.set('type', type)
    if (status) params.set('status', status)
    if (search.trim()) params.set('q', search.trim())
    return params.toString()
  }, [scope, sort, type, status, search])

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      Promise.all([
        apiRequest<{ items: ApprovalItem[]; total: number }>(`/api/v1/approvals?${query}`, {}, tenantId),
        apiRequest<Summary>('/api/v1/approvals/summary', {}, tenantId),
      ])
        .then(([page, stats]) => {
          if (!active) return
          setItems(page.items)
          setTotal(page.total)
          setSummary(stats)
          setError('')
        })
        .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
        .finally(() => active && setLoading(false))
    }, 200)
    return () => { active = false; window.clearTimeout(timer) }
  }, [query, tenantId, reload, t])

  const refresh = useCallback(() => { setReload((value) => value + 1); onChanged() }, [onChanged])

  const cards: { key: string; label: string; value: string; scope?: Scope }[] = [
    { key: 'pending', label: t('cards.pending'), value: String(summary?.pending_mine ?? '—'), scope: 'pending_mine' },
    { key: 'submitted', label: t('cards.submitted'), value: String(summary?.submitted ?? '—'), scope: 'submitted' },
    { key: 'completed', label: t('cards.completed'), value: String(summary?.completed_this_month ?? '—'), scope: 'all' },
    { key: 'avg', label: t('cards.avg'), value: duration(summary?.avg_duration_seconds ?? null) },
  ]

  return (
    <section className="settings-page approvals-page">
      <div className="page-heading"><div><h1>{t('title')}</h1><p>{t('subtitle')}</p></div></div>
      <div className="approval-cards">
        {cards.map((card) => card.scope
          ? <button aria-pressed={scope === card.scope && (card.key !== 'completed' || status === 'done')} className={`approval-card${scope === card.scope && card.key !== 'completed' ? ' selected' : ''}`} key={card.key} onClick={() => { setScope(card.scope!); setStatus('') }} type="button"><small>{card.label}</small><strong>{card.value}</strong></button>
          : <div className="approval-card" key={card.key}><small>{card.label}</small><strong>{card.value}</strong></div>)}
      </div>

      <div className="assistants-toolbar">
        <label className="assistants-search"><Search size={15} /><input onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></label>
        <select aria-label={t('filters.type')} onChange={(event) => setType(event.target.value)} value={type}>
          <option value="">{t('filters.allTypes')}</option>
          {TYPES.map((key) => <option key={key} value={key}>{t(`types.${key}`)}</option>)}
        </select>
        <select aria-label={t('filters.status')} onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">{t('filters.allStatuses')}</option>
          {STATUSES.map((key) => <option key={key} value={key}>{t(`status.${key}`)}</option>)}
        </select>
        <select aria-label={t('filters.sort')} onChange={(event) => setSort(event.target.value)} value={sort}>
          <option value="oldest">{t('filters.oldest')}</option>
          <option value="newest">{t('filters.newest')}</option>
        </select>
        <div className="approval-scope" role="group" aria-label={t('filters.scope')}>
          {(['pending_mine', 'submitted', 'all'] as const).map((key) => (
            <button aria-pressed={scope === key} className={scope === key ? 'selected' : ''} key={key} onClick={() => setScope(key)} type="button">{t(`scope.${key}`)}</button>
          ))}
        </div>
      </div>

      {error && <p className="form-error" role="alert">{error} <button className="text-button" onClick={refresh} type="button">{t('retry')}</button></p>}
      {loading && <p>{t('loading')}</p>}
      {!loading && !error && items.length === 0 && <p className="assistants-empty-filtered">{t('empty')}</p>}
      {items.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('table.object')}</th><th>{t('table.requester')}</th><th>{t('table.organization')}</th><th>{t('table.submitted')}</th><th>{t('table.checks')}</th><th>{t('table.status')}</th><th /></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td><strong>{item.title}</strong><small className="platform-tenant-slug">{t(`types.${item.type}`)}</small></td>
                  <td>{item.requester_name || '—'}</td>
                  <td>{item.organization_name || '—'}</td>
                  <td>{formatRelativeTime(item.submitted_at)}</td>
                  <td>{item.checks.map((check) => <span className={`check-chip ${check.level ?? 'ok'}`} key={check.label}>{check.label}</span>)}</td>
                  <td><StatusBadge item={item} /></td>
                  <td><button className="text-button" onClick={() => setOpenId(item.id)} type="button">{item.can_decide ? t('review') : t('view')}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <small className="wizard-muted">{t('total', { count: total })}</small>
        </div>
      )}

      {openId && <ReviewDrawer id={openId} onChanged={refresh} onClose={() => setOpenId(null)} tenantId={tenantId} writable={writable} />}
    </section>
  )
}

function StatusBadge({ item }: { item: ApprovalItem }) {
  const { t } = useTranslation('approvals')
  const failed = item.status === 'approved' && item.apply_status === 'failed'
  const label = failed ? t('applyFailed') : item.status === 'pending' && item.level_count > 1
    ? t('status.pendingLevel', { level: item.current_level, total: item.level_count })
    : t(`status.${item.status}`)
  return <span className={`approval-status ${failed ? 'failed' : item.status}`}>{label}</span>
}

function ReviewDrawer({ id, tenantId, writable, onClose, onChanged }: { id: string; tenantId: string; writable: boolean; onClose: () => void; onChanged: () => void }) {
  const { t } = useTranslation('approvals')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true
    apiRequest<Detail>(`/api/v1/approvals/${id}`, {}, tenantId)
      .then((result) => active && setDetail(result))
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [id, tenantId, t])

  async function act(action: 'approve' | 'reject' | 'withdraw' | 'comment' | 'retry') {
    setBusy(true)
    setError('')
    try {
      const body = action === 'comment' ? { body: comment } : { comment }
      setDetail(await apiRequest<Detail>(`/api/v1/approvals/${id}/${action}`, { method: 'POST', body: JSON.stringify(body) }, tenantId))
      setComment('')
      onChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <aside aria-labelledby="review-title" aria-modal="true" className="create-modal review-drawer" onClick={(event) => event.stopPropagation()} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        {!detail && !error && <p>{t('loading')}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {detail && (
          <>
            <h2 id="review-title">{detail.title}</h2>
            <p className="wizard-muted">{t(`types.${detail.type}`)} · <StatusBadge item={detail} /> · {t('by', { name: detail.requester_name || '—', time: formatRelativeTime(detail.submitted_at) })}</p>
            {detail.description && <p>{detail.description}</p>}
            {detail.apply_error && <p className="form-error" role="alert">{t('applyError', { error: detail.apply_error })}</p>}

            {detail.change_diff.length > 0 && <><h3>{t('diff')}</h3><ChangeList changes={detail.change_diff} /></>}
            {detail.checks.length > 0 && <><h3>{t('checks')}</h3><p>{detail.checks.map((check) => <span className={`check-chip ${check.level ?? 'ok'}`} key={check.label}>{check.label}</span>)}</p></>}

            <h3>{t('progress')}</h3>
            <ol className="approval-levels">
              {detail.levels.map((level) => (
                <li key={level.level}>
                  <strong>{t('level', { level: level.level })}</strong> <span className={`approval-status ${level.status}`}>{t(`levelStatus.${level.status}`)}</span>
                  <small>{level.decided_by ? t('decidedBy', { name: level.decided_by, time: level.decided_at ? formatRelativeTime(level.decided_at) : '' }) : t('waiting', { names: level.approvers.join('、') })}</small>
                  {level.comment && <p>{level.comment}</p>}
                </li>
              ))}
            </ol>

            <h3>{t('comments')}</h3>
            {detail.comments.length === 0 && <p className="wizard-muted">{t('noComments')}</p>}
            <ul className="approval-comments">{detail.comments.map((item) => <li key={item.id}><strong>{item.author_name}</strong> <small>{formatRelativeTime(item.created_at)}</small><p>{item.body}</p></li>)}</ul>

            {writable && (
              <>
                <label className="publish-note">{t('commentLabel')}
                  <textarea maxLength={1000} onChange={(event) => setComment(event.target.value)} placeholder={detail.can_decide ? t('commentPlaceholderDecide') : t('commentPlaceholder')} rows={3} value={comment} />
                </label>
                <div className="confirm-actions">
                  <button className="secondary-button" disabled={busy || !comment.trim()} onClick={() => void act('comment')} type="button">{t('actions.comment')}</button>
                  {detail.can_withdraw && <button className="secondary-button" disabled={busy} onClick={() => void act('withdraw')} type="button">{t('actions.withdraw')}</button>}
                  {detail.can_retry && <button className="secondary-button" disabled={busy} onClick={() => void act('retry')} type="button">{t('actions.retry')}</button>}
                  {detail.can_decide && <button className="secondary-button danger" disabled={busy || !comment.trim()} onClick={() => void act('reject')} title={t('rejectNeedsComment')} type="button">{t('actions.reject')}</button>}
                  {detail.can_decide && <button className="primary-button" disabled={busy} onClick={() => void act('approve')} type="button">{t('actions.approve')}</button>}
                </div>
              </>
            )}
          </>
        )}
      </aside>
    </div>
  )
}
