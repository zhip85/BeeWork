import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
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

const STATUS_OPTIONS = [['', '全部'], ['queued', '排队中'], ['sent', '已发送'], ['failed', '失败']] as const

const STATUS_LABELS: Record<string, string> = { queued: '排队中', sent: '已发送', failed: '失败' }

/** 邮件投递记录（T-008）：收件人只显示掩码，正文不入库；失败的任务在「后台任务」页可重试。 */
export function PlatformMailPage() {
  const [deliveries, setDeliveries] = useState<MailDelivery[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async (status: string) => {
    setLoading(true)
    try {
      const query = status ? `&status=${encodeURIComponent(status)}` : ''
      const result = await apiRequest<DeliveryPage>(`/api/v1/platform/mail/deliveries?limit=100${query}`)
      setDeliveries(result.items)
      setError('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '无法加载投递记录')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let active = true
    apiRequest<DeliveryPage>('/api/v1/platform/mail/deliveries?limit=100')
      .then((result) => active && setDeliveries(result.items))
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [])

  return <section className="settings-page">
    <div className="page-heading"><div><p className="section-kicker">运维</p><h1>邮件投递记录</h1><p>查看系统发出的邀请与通知邮件的投递状态；收件人仅显示掩码。失败后任务会自动退避重试，重试耗尽后可在「后台任务」页重试。</p></div><button className="secondary-button" type="button" onClick={() => void load(statusFilter)}><RefreshCw size={15} />刷新</button></div>
    <div className="filter-row">
      <label>状态<select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); void load(event.target.value) }}>{STATUS_OPTIONS.map(([value, label]) => <option key={value || 'all'} value={value}>{label}</option>)}</select></label>
    </div>
    {loading ? <p>正在加载投递记录…</p> : error ? <p className="form-error">{error}</p> : <div className="table-wrap"><table className="data-table"><thead><tr><th>收件人</th><th>模板</th><th>语言</th><th>状态</th><th>尝试</th><th>失败原因</th><th>发送时间</th><th>创建时间</th></tr></thead><tbody>{deliveries.length === 0 ? <tr><td colSpan={8}>暂无投递记录</td></tr> : deliveries.map((item) => <tr key={item.id}><td>{item.recipient_masked}</td><td>{item.template}</td><td>{item.locale}</td><td>{STATUS_LABELS[item.status] ?? item.status}</td><td>{item.attempts}</td><td>{item.error ?? '—'}</td><td>{item.sent_at ? new Date(item.sent_at).toLocaleString() : '—'}</td><td>{new Date(item.created_at).toLocaleString()}</td></tr>)}</tbody></table></div>}
  </section>
}
