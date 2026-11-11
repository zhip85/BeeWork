import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'

type Grant = { mode: 'named' | 'active' | 'unlimited'; seats: number | null; overage: string | null; assignable_limit: number | null }
type Summary = {
  grant: Grant; assigned: number; remaining: number | null; requested: number; idle: number; over_contract: boolean
  console_seats_used: number; console_seats_limit: number | null
}
type Member = {
  user_id: string; display_name: string; email: string; organization_name: string; role: string
  state: 'assigned' | 'requested' | 'unassigned'; source: 'rule' | 'manual' | 'auto' | null; reason: string | null
  last_login_at: string | null; idle: boolean
}
type Rule = { organization_id: string; name: string; parent_id: string | null; member_count: number; enabled: boolean }
type Policy = { auto_assign: boolean; idle_days: 30 | 60 | 90 | null }
type Filter = '' | 'assigned' | 'unassigned' | 'requested' | 'idle'
type Change = { changed: number; notice: string | null; summary: Summary }

const PAGE_SIZE = 20

/** Portal members and seat assignment (演进 PRD 5.2, 13.4). The grant is the platform's and read-only here; every limit
 * is enforced by the server, this page only shows its numbers and answers. */
export function MembersPanel({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('portal')
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && tenant.permissions.includes('portal:edit')
  const canAssign = writable && tenant.permissions.includes('portal:publish')
  const [summary, setSummary] = useState<Summary | null>(null)
  const [rules, setRules] = useState<Rule[]>([])
  const [policy, setPolicy] = useState<Policy>({ auto_assign: false, idle_days: null })
  const [members, setMembers] = useState<Member[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState<Filter>('')
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const loadAll = useCallback(() => {
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (filter) params.set('filter', filter)
    if (search.trim()) params.set('q', search.trim())
    Promise.all([
      apiRequest<Summary>('/api/v1/portal/seats/summary', {}, tenant.id),
      apiRequest<{ organizations: Rule[] }>('/api/v1/portal/seats/rules', {}, tenant.id),
      apiRequest<Policy>('/api/v1/portal/seats/policy', {}, tenant.id),
      apiRequest<{ items: Member[]; total: number }>(`/api/v1/portal/seats/members?${params}`, {}, tenant.id),
    ])
      .then(([counts, ruleResult, policyResult, list]) => {
        setSummary(counts); setRules(ruleResult.organizations); setPolicy(policyResult)
        setMembers(list.items); setTotal(list.total); setError('')
      })
      .catch((failure: Error) => setError(failure.message || t('loadFailed')))
  }, [filter, page, search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(loadAll, 200)
    return () => window.clearTimeout(timer)
  }, [loadAll])

  async function run(request: () => Promise<Change | { reclaimed: number; summary: Summary }>, done: (result: never) => string) {
    setBusy(true); setNotice(''); setError('')
    try {
      const result = await request()
      setNotice(done(result as never))
      setPicked(new Set())
      loadAll()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('actionFailed'))
    } finally {
      setBusy(false)
    }
  }
  const post = (action: string, ids: string[]) => () =>
    apiRequest<Change>(`/api/v1/portal/seats/${action}`, { method: 'POST', body: JSON.stringify({ user_ids: ids }) }, tenant.id)
  const batch = (action: 'assign' | 'revoke' | 'approve' | 'reject', ids: string[]) =>
    run(post(action, ids), (result: Change) => `${t(`members.done.${action}`, { count: result.changed })}${result.notice ? ` · ${result.notice}` : ''}`)

  function toggleRule(rule: Rule) {
    const enabled = rules.filter((item) => item.enabled).map((item) => item.organization_id)
    const next = rule.enabled ? enabled.filter((id) => id !== rule.organization_id) : [...enabled, rule.organization_id]
    void run(
      () => apiRequest<Change>('/api/v1/portal/seats/rules', { method: 'PUT', body: JSON.stringify({ organization_ids: next }) }, tenant.id),
      (result: Change) => result.notice ?? t('members.done.rules'),
    )
  }
  async function savePolicy(next: Policy) {
    setPolicy(next)
    try {
      await apiRequest<Policy>('/api/v1/portal/seats/policy', { method: 'PUT', body: JSON.stringify(next) }, tenant.id)
      setError(''); loadAll()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('actionFailed')); loadAll()
    }
  }

  const grant = summary?.grant
  const limit = grant?.seats ?? null
  const percent = limit ? Math.min(100, Math.round(((summary?.assigned ?? 0) / limit) * 100)) : 0
  const visibleIds = members.map((member) => member.user_id)
  const allPicked = visibleIds.length > 0 && visibleIds.every((id) => picked.has(id))
  const chosen = members.filter((member) => picked.has(member.user_id))  // only what is checked AND currently shown
  const pick = (id: string) => setPicked((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })
  const stateButton = (key: Filter, label: string, value: string | number, hint?: ReactNode) => (
    <button aria-pressed={filter === key} className={`portal-seat-card${filter === key ? ' selected' : ''}`} onClick={() => { setFilter(filter === key ? '' : key); setPage(1) }} type="button">
      <small>{label}</small><strong>{value}</strong>{hint}
    </button>
  )

  return (
    <div className="portal-members">
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {grant && (
        <div className="portal-grant-bar">
          <div>
            <small>{t('members.grantTitle')}</small>
            <strong>{t(`members.modes.${grant.mode}`)}{grant.seats != null && ` · ${grant.seats.toLocaleString()}`}{grant.overage && ` · ${t(`members.overages.${grant.overage}`)}`}</strong>
            <small>{t('members.grantNote')}</small>
          </div>
          <div>
            <small>{t('members.consoleSeats')}</small>
            <strong>{summary?.console_seats_used} / {summary?.console_seats_limit ?? t('members.unlimited')}</strong>
          </div>
        </div>
      )}
      {summary && (
        <div className="portal-seat-cards">
          {stateButton('assigned', t('members.assigned'), grant?.mode === 'unlimited' ? `${summary.assigned}` : `${summary.assigned} / ${limit?.toLocaleString()}`,
            limit ? <span aria-hidden="true" className="portal-seat-bar"><i style={{ width: `${percent}%` }} /></span> : undefined)}
          {stateButton('requested', t('members.requested'), summary.requested)}
          {stateButton('idle', t('members.idle'), summary.idle)}
        </div>
      )}
      {summary?.over_contract && <p className="assistants-notice" role="status">{t('members.overContract', { seats: limit })}</p>}

      <div className="detail-card portal-seat-rules">
        <h3>{t('members.rules')}</h3>
        <p className="portal-hint">{t('members.rulesHint')}</p>
        <div className="portal-rule-list">
          {rules.map((rule) => (
            <label key={rule.organization_id}>
              <input checked={rule.enabled} disabled={!canEdit || busy} onChange={() => toggleRule(rule)} type="checkbox" />
              <span>{rule.name}</span><small>{t('members.people', { count: rule.member_count })}</small>
            </label>
          ))}
        </div>
        <div className="portal-policy">
          <label>
            <input checked={policy.auto_assign} disabled={!canEdit} onChange={(event) => void savePolicy({ ...policy, auto_assign: event.target.checked })} type="checkbox" />
            {t('members.autoAssign')}
          </label>
          <label>{t('members.idleDays')}
            <select disabled={!canEdit} onChange={(event) => void savePolicy({ ...policy, idle_days: event.target.value ? (Number(event.target.value) as Policy['idle_days']) : null })} value={policy.idle_days ?? ''}>
              <option value="">{t('members.never')}</option>
              {[30, 60, 90].map((days) => <option key={days} value={days}>{t('members.days', { count: days })}</option>)}
            </select>
          </label>
          <button className="secondary-button" disabled={!canAssign || busy || !summary?.idle}
            onClick={() => void run(() => apiRequest('/api/v1/portal/seats/reclaim-idle', { method: 'POST', body: '{}' }, tenant.id), (result: { reclaimed: number }) => t('members.done.reclaim', { count: result.reclaimed }))} type="button">
            {t('members.reclaim', { count: summary?.idle ?? 0 })}
          </button>
        </div>
      </div>

      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('members.search')} onChange={(event) => { setSearch(event.target.value); setPage(1) }} placeholder={t('members.search')} value={search} /></label>
        <select aria-label={t('members.filter')} onChange={(event) => { setFilter(event.target.value as Filter); setPage(1) }} value={filter}>
          <option value="">{t('members.filters.all')}</option>
          {(['assigned', 'unassigned', 'requested', 'idle'] as const).map((key) => <option key={key} value={key}>{t(`members.filters.${key}`)}</option>)}
        </select>
        {canAssign && chosen.length > 0 && (
          <span className="portal-batch">
            <small>{t('members.picked', { count: chosen.length })}</small>
            {chosen.some((member) => member.state === 'unassigned') && <button className="secondary-button" disabled={busy} onClick={() => void batch('assign', chosen.filter((m) => m.state === 'unassigned').map((m) => m.user_id))} type="button">{t('members.assign')}</button>}
            {chosen.some((member) => member.state === 'assigned') && <button className="secondary-button" disabled={busy} onClick={() => void batch('revoke', chosen.filter((m) => m.state === 'assigned').map((m) => m.user_id))} type="button">{t('members.revoke')}</button>}
            {chosen.some((member) => member.state === 'requested') && <button className="secondary-button" disabled={busy} onClick={() => void batch('approve', chosen.filter((m) => m.state === 'requested').map((m) => m.user_id))} type="button">{t('members.approve')}</button>}
            {chosen.some((member) => member.state === 'requested') && <button className="secondary-button" disabled={busy} onClick={() => void batch('reject', chosen.filter((m) => m.state === 'requested').map((m) => m.user_id))} type="button">{t('members.reject')}</button>}
          </span>
        )}
      </div>
      <div className="members-table-wrap">
        <table className="members-table">
          <thead><tr>
            <th>{canAssign && <input aria-label={t('members.pickAll')} checked={allPicked} onChange={() => setPicked(allPicked ? new Set() : new Set(visibleIds))} type="checkbox" />}</th>
            <th>{t('members.col.member')}</th><th>{t('members.col.source')}</th><th>{t('members.col.login')}</th><th>{t('members.col.state')}</th><th>{t('members.col.actions')}</th>
          </tr></thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.user_id}>
                <td>{canAssign && <input aria-label={member.display_name} checked={picked.has(member.user_id)} onChange={() => pick(member.user_id)} type="checkbox" />}</td>
                <td><span className="platform-admin-cell"><strong>{member.display_name}</strong><small>{member.email} · {member.organization_name}</small></span></td>
                <td>{member.state === 'requested' ? (member.reason || '—') : member.source ? t(`members.sources.${member.source}`) : '—'}</td>
                <td>{member.last_login_at ? new Date(member.last_login_at).toLocaleDateString() : '—'}{member.idle && <small className="portal-idle"> {t('members.idleTag')}</small>}</td>
                <td><span className={`member-status ${member.state === 'assigned' ? 'active' : 'inactive'}`}><i />{t(`members.states.${member.state}`)}</span></td>
                <td className="member-actions-cell">
                  {canAssign && member.state === 'unassigned' && <button className="tenant-status-action" disabled={busy} onClick={() => void batch('assign', [member.user_id])} type="button">{t('members.assign')}</button>}
                  {canAssign && member.state === 'assigned' && <button className="tenant-status-action" disabled={busy} onClick={() => void batch('revoke', [member.user_id])} type="button">{t('members.revoke')}</button>}
                  {canAssign && member.state === 'requested' && <><button className="tenant-status-action" disabled={busy} onClick={() => void batch('approve', [member.user_id])} type="button">{t('members.approve')}</button>{' '}<button className="tenant-status-action" disabled={busy} onClick={() => void batch('reject', [member.user_id])} type="button">{t('members.reject')}</button></>}
                </td>
              </tr>
            ))}
            {members.length === 0 && <tr><td colSpan={6}><div className="members-empty">{t('members.empty')}</div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="portal-pager">
        <small>{t('members.total', { count: total })}</small>
        <button className="secondary-button" disabled={page <= 1} onClick={() => setPage(page - 1)} type="button">{t('members.prev')}</button>
        <button className="secondary-button" disabled={page * PAGE_SIZE >= total} onClick={() => setPage(page + 1)} type="button">{t('members.next')}</button>
      </div>
    </div>
  )
}
