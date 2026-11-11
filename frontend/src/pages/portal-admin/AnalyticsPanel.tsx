import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'

type Metric = { value: number | null; previous: number | null; change: number | null }
type Analytics = {
  days: number; active_employees: Metric; calls: Metric; success_rate: Metric; satisfaction: Metric; ratings_up: number; ratings_down: number
  top_apps: { app_id: string | null; name: string; calls: number; success_rate: number | null; employees: number }[]
  trend: { day: string; calls: number; employees: number }[]
  seats: { mode: string; contract_seats: number | null; assigned: number; requested: number; idle: number; monthly_active_employees: number; over_active_limit: boolean }
}
const RANGES = [7, 30, 90] as const

/** Usage analytics (演进 PRD 10.5): read-only numbers computed from the real portal records. */
export function AnalyticsPanel({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('portal')
  const [days, setDays] = useState<(typeof RANGES)[number]>(30)
  const [data, setData] = useState<Analytics | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<Analytics>(`/api/v1/portal/analytics?days=${days}`, {}, tenant.id)
      .then((result) => { if (active) { setData(result); setError('') } })
      .catch((failure: Error) => { if (active) setError(failure.message || t('loadFailed')) })
    return () => { active = false }
  }, [days, tenant.id, t])

  const percent = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`)
  const change = (metric: Metric) => {
    if (metric.change === null) return <small>{t('analytics.noChange')}</small>
    const up = metric.change >= 0
    return <small className={up ? 'portal-up' : 'portal-down'}>{up ? '▲' : '▼'} {Math.abs(Math.round(metric.change * 100))}%</small>
  }
  const peak = Math.max(1, ...(data?.trend.map((day) => day.calls) ?? [0]))

  return (
    <div className="portal-analytics">
      <div className="knowledge-toolbar">
        <div className="portal-chips" role="group" aria-label={t('analytics.range')}>
          {RANGES.map((range) => <button aria-pressed={days === range} className={days === range ? 'selected' : ''} key={range} onClick={() => setDays(range)} type="button">{t('analytics.lastDays', { count: range })}</button>)}
        </div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {data?.seats.over_active_limit && <p className="assistants-notice" role="status">{t('analytics.overActive', { active: data.seats.monthly_active_employees, seats: data.seats.contract_seats })}</p>}
      {data && (
        <>
          <div className="portal-stats">
            <div><small>{t('analytics.activeEmployees')}</small><strong>{data.active_employees.value ?? 0}</strong>{change(data.active_employees)}</div>
            <div><small>{t('analytics.calls')}</small><strong>{data.calls.value ?? 0}</strong>{change(data.calls)}</div>
            <div><small>{t('analytics.successRate')}</small><strong>{percent(data.success_rate.value)}</strong>{change(data.success_rate)}</div>
            <div><small>{t('analytics.satisfaction')}</small><strong>{percent(data.satisfaction.value)}</strong><small>{t('analytics.ratings', { up: data.ratings_up, down: data.ratings_down })}</small></div>
          </div>

          <div className="detail-card">
            <h3>{t('analytics.trend')}</h3>
            <div className="portal-trend" role="img" aria-label={t('analytics.trend')}>
              {data.trend.map((day) => (
                <span className="portal-trend-bar" key={day.day} title={`${day.day}: ${t('analytics.calls')} ${day.calls} · ${t('analytics.activeEmployees')} ${day.employees}`}>
                  <i style={{ height: `${Math.max(day.calls ? 4 : 0, (day.calls / peak) * 100)}%` }} />
                </span>
              ))}
            </div>
            <div className="portal-trend-axis"><small>{data.trend[0]?.day}</small><small>{data.trend[data.trend.length - 1]?.day}</small></div>
          </div>

          <div className="detail-card">
            <h3>{t('analytics.topApps')}</h3>
            {data.top_apps.length === 0 ? <p className="portal-hint">{t('analytics.noUsage')}</p> : (
              <table className="members-table">
                <thead><tr><th>{t('analytics.app')}</th><th>{t('analytics.calls')}</th><th>{t('analytics.activeEmployees')}</th><th>{t('analytics.successRate')}</th></tr></thead>
                <tbody>{data.top_apps.map((app) => <tr key={app.app_id ?? app.name}><td>{app.name}</td><td>{app.calls}</td><td>{app.employees}</td><td>{percent(app.success_rate)}</td></tr>)}</tbody>
              </table>
            )}
          </div>

          <div className="portal-stats">
            <div><small>{t('analytics.assigned')}</small><strong>{data.seats.assigned}{data.seats.contract_seats ? ` / ${data.seats.contract_seats.toLocaleString()}` : ''}</strong></div>
            <div><small>{t('analytics.requested')}</small><strong>{data.seats.requested}</strong></div>
            <div><small>{t('analytics.idle')}</small><strong>{data.seats.idle}</strong></div>
            <div><small>{t('analytics.monthlyActive')}</small><strong>{data.seats.monthly_active_employees}</strong></div>
          </div>
          <p className="portal-hint">{t('analytics.definitions')}</p>
        </>
      )}
    </div>
  )
}
