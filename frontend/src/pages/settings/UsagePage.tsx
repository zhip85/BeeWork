import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Download } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { formatDateTime } from '../../i18n/format'
import { SettingsTabs } from './SettingsTabs'

type Budget = {
  monthly_points: number
  thresholds: number[]
  recipients: string[]
  pause_non_production: boolean
  percent_used: number
  paused: boolean
}
type Summary = {
  month: string
  points_used: number
  points_quota: number | null
  projected_points: number
  seats_used: number
  seats_limit: number | null
  environment_count: number
  plan_name: string
  billing_cycle: 'monthly' | 'yearly'
  renews_at: string | null
  budget: Budget
  history: { month: string; points: number }[]
  by_assistant: { assistant_id: string | null; name: string | null; points: number; share: number; other: boolean }[]
  synced_at: string | null
  sync_error: string | null
}
const THRESHOLDS = [50, 80, 100] as const

function today() { return new Date().toISOString().slice(0, 10) }
function monthStart() { return `${today().slice(0, 7)}-01` }

/** Subscription and usage (SET-080–082): this month's points, the 6-month trend, the budget and the CSV export. */
export function UsagePage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const { t, i18n } = useTranslation('settings')
  const [summary, setSummary] = useState<Summary | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [budgetPoints, setBudgetPoints] = useState('')
  const [thresholds, setThresholds] = useState<number[]>([])
  const [recipients, setRecipients] = useState('')
  const [pause, setPause] = useState(false)
  const [saving, setSaving] = useState(false)
  const [from, setFrom] = useState(monthStart())
  const [to, setTo] = useState(today())
  const [exporting, setExporting] = useState(false)
  const number = useCallback((value: number) => value.toLocaleString(i18n.language), [i18n.language])

  const load = useCallback(() => {
    apiRequest<Summary>('/api/v1/tenant/usage', {}, tenantId)
      .then((result) => {
        setSummary(result)
        setBudgetPoints(String(result.budget.monthly_points))
        setThresholds(result.budget.thresholds)
        setRecipients(result.budget.recipients.join(', '))
        setPause(result.budget.pause_non_production)
        setError('')
      })
      .catch((requestError: Error) => setError(requestError.message || t('usage.loadFailed')))
  }, [tenantId, t])

  useEffect(() => { load() }, [load])

  async function saveBudget(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    try {
      await apiRequest('/api/v1/tenant/usage/budget', {
        method: 'PUT',
        body: JSON.stringify({
          monthly_points: Number(budgetPoints),
          thresholds,
          recipients: recipients.split(/[,;\s]+/).filter(Boolean),
          pause_non_production: pause,
        }),
      }, tenantId)
      setNotice(t('usage.budget.saved'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('usage.budget.saveFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function exportCsv() {
    setExporting(true)
    setError('')
    try {
      const response = await fetch(`/api/v1/tenant/usage/export?start=${from}&end=${to}`, {
        credentials: 'same-origin',
        headers: { 'X-Tenant-ID': tenantId },
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(typeof payload.detail === 'string' ? payload.detail : t('usage.exportFailed'))
      }
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `beework-usage-${from}_${to}.csv`
      anchor.click()
      URL.revokeObjectURL(url)
      setNotice(t('usage.exported'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('usage.exportFailed'))
    } finally {
      setExporting(false)
    }
  }

  const peak = summary ? Math.max(1, summary.budget.monthly_points, ...summary.history.map((item) => item.points)) : 1
  const quotaPercent = summary?.points_quota ? Math.min(100, (summary.points_used / summary.points_quota) * 100) : 0

  return (
    <section className="settings-page usage-page">
      <SettingsTabs />
      <div className="page-heading"><div><h1>{t('usage.title')}</h1><p>{t('usage.subtitle')}</p></div></div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {!summary && !error && <p>{t('usage.loading')}</p>}
      {summary && (
        <>
          <div className="detail-card usage-plan">
            <div>
              <h2>{t('usage.currentPlan')}</h2>
              <strong className="usage-plan-name">{summary.plan_name} · {t(`usage.cycle.${summary.billing_cycle}`)}</strong>
              <small>
                {summary.renews_at ? t('usage.renews', { date: formatDateTime(summary.renews_at) }) : t('usage.noRenewal')}
                {' · '}{t('usage.seatsCount', { count: summary.seats_limit ?? 0 })}
                {' · '}{t('usage.environmentsCount', { count: summary.environment_count })}
              </small>
            </div>
            <p className="usage-plan-note">{t('usage.changePlanNote')}</p>
          </div>

          <div className="usage-tiles">
            <div className="detail-card usage-tile">
              <small>{t('usage.pointsUsed')}</small>
              <strong>{number(summary.points_used)}{summary.points_quota !== null && <span> / {number(summary.points_quota)}</span>}</strong>
              {summary.points_quota !== null && (
                <div aria-label={t('usage.pointsUsed')} aria-valuemax={100} aria-valuemin={0} aria-valuenow={Math.round(quotaPercent)} className="usage-meter" role="progressbar"><i style={{ width: `${quotaPercent}%` }} /></div>
              )}
              {summary.points_quota === null && <small>{t('usage.unlimited')}</small>}
            </div>
            <div className="detail-card usage-tile">
              <small>{t('usage.seats')}</small>
              <strong>{summary.seats_used}{summary.seats_limit !== null && <span> / {summary.seats_limit}</span>}</strong>
            </div>
            <div className="detail-card usage-tile">
              <small>{t('usage.projected')}</small>
              <strong>{number(summary.projected_points)}</strong>
              <small>{t('usage.projectedHint')}</small>
            </div>
          </div>
          <p className="usage-sync">
            {summary.synced_at ? t('usage.syncedAt', { time: formatDateTime(summary.synced_at) }) : t('usage.neverSynced')}
            {summary.sync_error && <span className="form-error"> · {t('usage.syncError')}</span>}
          </p>

          <div className="detail-card">
            <h2>{t('usage.trend')}</h2>
            <p className="usage-hint">{t('usage.trendHint')}</p>
            <div className="usage-chart" role="img" aria-label={t('usage.trend')}>
              <div className="usage-budget-line" style={{ bottom: `${(summary.budget.monthly_points / peak) * 100}%` }}><span>{t('usage.budgetLine', { value: number(summary.budget.monthly_points) })}</span></div>
              {summary.history.map((item) => (
                <div className="usage-bar-col" key={item.month}>
                  <span className="usage-bar-value">{number(item.points)}</span>
                  <i className={`usage-bar${item.points > summary.budget.monthly_points ? ' over' : ''}`} style={{ height: `${Math.max(2, (item.points / peak) * 100)}%` }} />
                  <small>{item.month}</small>
                </div>
              ))}
            </div>
          </div>

          <div className="detail-card">
            <h2>{t('usage.byAssistant')}</h2>
            {summary.by_assistant.length === 0 && <p className="usage-hint">{t('usage.byAssistantEmpty')}</p>}
            {summary.by_assistant.map((item, index) => (
              <div className="usage-row" key={item.assistant_id ?? `x${index}`}>
                <span>{item.other ? t('usage.other') : item.name ?? t('usage.unattributed')}</span>
                <div className="usage-meter"><i style={{ width: `${Math.round(item.share * 100)}%` }} /></div>
                <span>{number(item.points)} <small>({Math.round(item.share * 100)}%)</small></span>
              </div>
            ))}
          </div>

          <form className="detail-card usage-budget" onSubmit={(event) => void saveBudget(event)}>
            <h2>{t('usage.budget.title')}</h2>
            <p className="usage-hint">{t('usage.budget.used', { percent: summary.budget.percent_used })}</p>
            <div aria-valuemax={100} aria-valuemin={0} aria-valuenow={Math.min(100, Math.round(summary.budget.percent_used))} className={`usage-meter${summary.budget.percent_used >= 80 ? ' warn' : ''}`} role="progressbar"><i style={{ width: `${Math.min(100, summary.budget.percent_used)}%` }} /></div>
            {summary.budget.paused && <p className="form-error" role="status">{t('usage.budget.paused')}</p>}
            <label>{t('usage.budget.monthly')}
              <input disabled={!canEdit} max={1000000000} min={1} onChange={(event) => setBudgetPoints(event.target.value)} required step={1} type="number" value={budgetPoints} />
            </label>
            <fieldset disabled={!canEdit}><legend>{t('usage.budget.thresholds')}</legend>
              {THRESHOLDS.map((value) => (
                <label className="wizard-radio" key={value}>
                  <input checked={thresholds.includes(value)} onChange={(event) => setThresholds(event.target.checked ? [...thresholds, value].sort((a, b) => a - b) : thresholds.filter((item) => item !== value))} type="checkbox" /> {value}%
                </label>
              ))}
              <small>{t('usage.budget.thresholdsHint')}</small>
            </fieldset>
            <label>{t('usage.budget.recipients')}
              <input disabled={!canEdit} onChange={(event) => setRecipients(event.target.value)} placeholder="ops@example.com, finance@example.com" value={recipients} />
              <small>{t('usage.budget.recipientsHint')}</small>
            </label>
            <label className="wizard-radio">
              <input checked={pause} disabled={!canEdit} onChange={(event) => setPause(event.target.checked)} type="checkbox" /> {t('usage.budget.pause')}
            </label>
            <small>{t('usage.budget.pauseHint')}</small>
            {canEdit
              ? <button className="primary-button" disabled={saving || !budgetPoints} type="submit">{saving ? t('usage.budget.saving') : t('usage.budget.save')}</button>
              : <small>{t('usage.budget.readOnly')}</small>}
          </form>

          <div className="detail-card usage-export">
            <h2>{t('usage.export.title')}</h2>
            <p className="usage-hint">{t('usage.export.hint')}</p>
            <div className="usage-export-row">
              <label>{t('usage.export.from')}<input max={to} onChange={(event) => setFrom(event.target.value)} type="date" value={from} /></label>
              <label>{t('usage.export.to')}<input min={from} onChange={(event) => setTo(event.target.value)} type="date" value={to} /></label>
              <button className="primary-button" disabled={exporting || !from || !to} onClick={() => void exportCsv()} type="button"><Download size={14} /> {exporting ? t('usage.export.exporting') : t('usage.export.action')}</button>
            </div>
          </div>
        </>
      )}
    </section>
  )
}
