import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

type MailDelivery = {
  id: string
  tenant_id: string | null
  template: string
  locale: string
  recipient_masked: string
  status: string
  error: string | null
  attempts: number
  sent_at: string | null
  created_at: string
}

type DeliveryPage = { items: MailDelivery[]; total: number }

const STATUSES = ['queued', 'sent', 'failed'] as const
const FAILURE_CODES = ['recipient_refused', 'smtp_auth_failed', 'smtp_unreachable', 'invalid_message']
const PAGE_SIZE = 100

/** 邮件投递记录（T-008）：收件人只显示掩码，正文不入库；失败的任务在「后台任务」页可重试。 */
export function PlatformMailPage() {
  const { t } = useTranslation('platform')
  const [deliveries, setDeliveries] = useState<MailDelivery[]>([])
  const [total, setTotal] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const latest = useRef(0)

  // Only the newest request may change the page: a slow answer to an earlier filter must not overwrite it.
  const load = async (status: string) => {
    const ticket = ++latest.current
    setLoading(true)
    try {
      const query = status ? `&status=${encodeURIComponent(status)}` : ''
      const result = await apiRequest<DeliveryPage>(`/api/v1/platform/mail/deliveries?limit=${PAGE_SIZE}${query}`)
      if (ticket !== latest.current) return
      setDeliveries(result.items)
      setTotal(result.total)
      setError('')
    } catch (requestError) {
      if (ticket !== latest.current) return
      setError(requestError instanceof Error ? requestError.message : t('mailPage.loadFailed'))
    } finally {
      if (ticket === latest.current) setLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load('')
  }, [])

  return <section className="settings-page">
    <div className="page-heading"><div><p className="section-kicker">{t('mailPage.kicker')}</p><h1>{t('mailPage.title')}</h1><p>{t('mailPage.intro')}</p></div><button className="secondary-button" type="button" onClick={() => void load(statusFilter)}><RefreshCw size={15} />{t('mailPage.refresh')}</button></div>
    <div className="filter-row">
      <label>{t('mailPage.status')}<select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); void load(event.target.value) }}><option value="">{t('mailPage.statusAll')}</option>{STATUSES.map((value) => <option key={value} value={value}>{t(`mailPage.statuses.${value}`)}</option>)}</select></label>
      {total > deliveries.length && <span className="wizard-muted">{t('mailPage.truncated', { shown: deliveries.length, total })}</span>}
    </div>
    {loading ? <p>{t('mailPage.loading')}</p> : error ? <p className="form-error">{error}</p> : <div className="table-wrap"><table className="data-table"><thead><tr><th>{t('mailPage.columns.recipient')}</th><th>{t('mailPage.columns.template')}</th><th>{t('mailPage.columns.locale')}</th><th>{t('mailPage.columns.status')}</th><th>{t('mailPage.columns.attempts')}</th><th>{t('mailPage.columns.error')}</th><th>{t('mailPage.columns.sentAt')}</th><th>{t('mailPage.columns.createdAt')}</th></tr></thead><tbody>{deliveries.length === 0 ? <tr><td colSpan={8}>{t('mailPage.empty')}</td></tr> : deliveries.map((item) => <tr key={item.id}><td>{item.recipient_masked}</td><td>{item.template}</td><td>{item.locale}</td><td>{(STATUSES as readonly string[]).includes(item.status) ? t(`mailPage.statuses.${item.status}`) : item.status}</td><td>{item.attempts}</td><td>{item.error ? (FAILURE_CODES.includes(item.error) ? t(`mailPage.failures.${item.error}`) : item.error) : '—'}</td><td>{item.sent_at ? new Date(item.sent_at).toLocaleString() : '—'}</td><td>{new Date(item.created_at).toLocaleString()}</td></tr>)}</tbody></table></div>}
  </section>
}
