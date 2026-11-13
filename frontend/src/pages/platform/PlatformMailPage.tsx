import { useEffect, useRef, useState } from 'react'
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

// The worker records a short code, never the provider's message (it can carry the address or server details).
const FAILURE_LABELS: Record<string, string> = {
  recipient_refused: '收件人被邮件服务拒绝', smtp_auth_failed: '邮件服务认证失败', smtp_unreachable: '无法连接邮件服务', invalid_message: '邮件内容无效（不会重试）',
}
const PAGE_SIZE = 100

/** 邮件投递记录（T-008）：收件人只显示掩码，正文不入库；失败的任务在「后台任务」页可重试。 */
export function PlatformMailPage() {
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
      setError(requestError instanceof Error ? requestError.message : '无法加载投递记录')
    } finally {
      if (ticket === latest.current) setLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load('')
  }, [])

  return <section className="settings-page">
    <div className="page-heading"><div><p className="section-kicker">运维</p><h1>邮件投递记录</h1><p>查看系统发出的邀请与通知邮件的投递状态；收件人仅显示掩码。失败后任务会自动退避重试，重试耗尽后可在「后台任务」页重试。</p></div><button className="secondary-button" type="button" onClick={() => void load(statusFilter)}><RefreshCw size={15} />刷新</button></div>
    <div className="filter-row">
      <label>状态<select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); void load(event.target.value) }}>{STATUS_OPTIONS.map(([value, label]) => <option key={value || 'all'} value={value}>{label}</option>)}</select></label>
      {total > deliveries.length && <span className="wizard-muted">仅显示最近 {deliveries.length} 条，共 {total} 条；请用状态筛选缩小范围</span>}
    </div>
    {loading ? <p>正在加载投递记录…</p> : error ? <p className="form-error">{error}</p> : <div className="table-wrap"><table className="data-table"><thead><tr><th>收件人</th><th>模板</th><th>语言</th><th>状态</th><th>尝试</th><th>失败原因</th><th>发送时间</th><th>创建时间</th></tr></thead><tbody>{deliveries.length === 0 ? <tr><td colSpan={8}>暂无投递记录</td></tr> : deliveries.map((item) => <tr key={item.id}><td>{item.recipient_masked}</td><td>{item.template}</td><td>{item.locale}</td><td>{STATUS_LABELS[item.status] ?? item.status}</td><td>{item.attempts}</td><td>{item.error ? (FAILURE_LABELS[item.error] ?? item.error) : '—'}</td><td>{item.sent_at ? new Date(item.sent_at).toLocaleString() : '—'}</td><td>{new Date(item.created_at).toLocaleString()}</td></tr>)}</tbody></table></div>}
  </section>
}
