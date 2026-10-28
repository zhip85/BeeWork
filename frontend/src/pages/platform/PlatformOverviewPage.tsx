import { useEffect, useState } from 'react'
import {
  Activity,
  ArrowRight,
  Building2,
  Boxes,
  ShieldCheck,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { IntegrationStatus, PlatformOverview, PlatformTenant } from '../../types'
import { enumLabel } from '../../i18n'

export function PlatformOverviewPage() {
  const [overview, setOverview] = useState<PlatformOverview | null>(null)
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [integrations, setIntegrations] = useState<IntegrationStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<PlatformOverview>('/api/v1/platform/overview'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
      apiRequest<IntegrationStatus[]>('/api/v1/platform/integrations'),
    ])
      .then(([summary, tenantList, integrationList]) => {
        if (!active) return
        setOverview(summary)
        setTenants(tenantList.slice(0, 5))
        setIntegrations(integrationList)
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  return (
    <section className="platform-overview">
      <div className="platform-page-heading">
        <div><div className="eyebrow"><span className="eyebrow-dot" />SaaS 管理平面</div><h1>平台总览</h1><p>查看 BeeWork 租户与账号的真实运行概况。</p></div>
        <Link className="primary-button" to="/platform/tenants"><Building2 size={16} />管理租户</Link>
      </div>
      {error && <div className="members-error" role="alert">{error}</div>}
      <div className="platform-metric-grid">
        <PlatformMetric label="租户总数" value={overview?.tenant_count} detail="已创建的企业工作区" icon={<Building2 size={18} />} />
        <PlatformMetric label="运行中" value={overview?.active_tenant_count} detail="当前可登录使用" icon={<Activity size={18} />} tone="green" />
        <PlatformMetric label="已停用" value={overview?.suspended_tenant_count} detail="平台暂停服务的企业" icon={<ShieldCheck size={18} />} tone="gold" />
        <PlatformMetric label="企业成员" value={overview?.active_member_count} detail={`账号关联共 ${overview?.member_count ?? '—'} 个席位`} icon={<Boxes size={18} />} tone="violet" />
        <PlatformMetric label="平台管理员" value={overview?.platform_admin_count} detail="仅限平台运营后台" icon={<ShieldCheck size={18} />} tone="gold" />
      </div>
      <div className="platform-overview-grid">
        <section className="panel platform-summary-panel">
          <div className="members-table-heading"><div><div className="section-kicker">租户生命周期</div><h2>最近创建的企业</h2></div><Link className="quiet-link" to="/platform/tenants">查看全部 <ArrowRight size={14} /></Link></div>
          {loading ? <div className="members-empty">正在读取平台数据…</div> : tenants.length === 0 ? <div className="members-empty">尚无企业工作区。创建租户后会显示在这里。</div> : (
            <div className="platform-recent-tenants">
              {tenants.map((tenant) => (
                <div className="platform-recent-row" key={tenant.id}>
                  <span className="platform-tenant-icon"><Building2 size={16} /></span>
                  <span className="platform-recent-copy"><strong>{tenant.name}</strong><small>{tenant.admin_email ?? '尚未设置管理员'} · {tenant.member_count} 位成员</small></span>
                  <span className={`member-status ${['trial', 'active', 'grace'].includes(tenant.status) ? 'active' : 'inactive'}`}><i />{enumLabel('tenantStatus', tenant.status)}</span>
                </div>
              ))}
            </div>
          )}
        </section>
        <aside className="platform-boundary-card">
          <span className="platform-boundary-symbol"><ShieldCheck size={20} /></span>
          <div className="section-kicker">平台与企业分权</div>
          <h2>平台管理员不等于企业管理员</h2>
          <p>平台账号管理租户状态和平台配置。每个新租户都单独设置首位企业管理员；该管理员使用自己的账号进入企业工作区。</p>
          <Link className="quiet-link" to="/platform/tenants">开始管理租户 <ArrowRight size={14} /></Link>
        </aside>
      </div>
      <section className="panel integration-panel">
        <div className="members-table-heading"><div><div className="section-kicker">引擎与基础设施</div><h2>第三方引擎接入状态</h2></div></div>
        <p className="organization-description">BeeWork 通过 HTTP API 调用这些开源引擎，不在平台内重复实现；连接地址与密钥在部署环境变量中配置。</p>
        <div className="integration-grid">
          {integrations.map((item) => {
            const state = !item.configured ? 'idle' : item.reachable ? 'ok' : 'error'
            return (
              <article className={`integration-card ${state}`} key={item.key}>
                <div className="integration-card-top"><strong>{item.name}</strong><span><i />{state === 'ok' ? '已连接' : state === 'error' ? '连接失败' : '未配置'}</span></div>
                <p>{item.purpose}</p>
                {item.detail && <small>{item.detail}</small>}
              </article>
            )
          })}
        </div>
      </section>
      <footer className="page-footer"><span>BeeWork SaaS · 平台运营后台</span><span>{loading ? '正在同步数据' : '数据来自当前平台数据库'}</span></footer>
    </section>
  )
}

export function PlatformMetric({
  label,
  value,
  detail,
  icon,
  tone = 'forest',
}: {
  label: string
  value: number | undefined
  detail: string
  icon: React.ReactNode
  tone?: string
}) {
  return (
    <article className="platform-metric-card">
      <div className="platform-metric-top"><span>{label}</span><i className={`platform-metric-icon ${tone}`}>{icon}</i></div>
      <strong>{value ?? '—'}</strong>
      <small>{detail}</small>
    </article>
  )
}
