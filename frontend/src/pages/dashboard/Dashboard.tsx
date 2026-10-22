import { useState } from 'react'
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  FileText,
  Plus,
  ShieldCheck,
  Sparkles,
  Workflow,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { CreateModal } from '../../components/CreateModal'
import type { CurrentUser, TenantMembership } from '../../types'

export function Dashboard({ user, tenant }: { user: CurrentUser; tenant: TenantMembership | null }) {
  const [createOpen, setCreateOpen] = useState(false)
  return (
    <>
      <section className="welcome-row">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />星期二 · 10 月 20 日</div>
          <h1>早上好，{user.display_name}<span className="greeting-comma">。</span></h1>
          <p>{tenant?.name ?? '企业工作区'} · 让企业知识与 AI 能力协同起来。</p>
        </div>
        <button className="primary-button" type="button" onClick={() => setCreateOpen(true)}>
          <Plus size={17} /> 创建助理
        </button>
      </section>

      <section className="metric-grid" aria-label="运行概况">
        <MetricCard label="本月会话" value="24,860" delta="+12.8%" icon={<Activity size={17} />} data={[34, 52, 40, 66, 48, 77, 58, 90]} />
        <MetricCard label="平均解决率" value="86.4%" delta="+4.2%" icon={<Check size={17} />} data={[42, 37, 58, 49, 68, 56, 76, 88]} />
        <MetricCard label="活跃助理" value="12" detail="共 16 个已创建" icon={<Bot size={17} />} />
        <MetricCard label="模型点数" value="1.28M" detail="本月已使用 64%" icon={<Sparkles size={17} />} />
      </section>

      <section className="content-grid">
        <div className="panel assistant-panel">
          <div className="panel-heading">
            <div>
              <div className="section-kicker">正在工作</div>
              <h2>热门助理</h2>
            </div>
            <Link className="quiet-link" to="/assistants">全部助理 <ArrowRight size={15} /></Link>
          </div>
          <div className="assistant-list">
            <AssistantRow initials="客" color="lilac" name="客户服务团队" subtitle="处理订单、售后与常见咨询" calls="8,420" rate="94.8%" trend="up" />
            <AssistantRow initials="保" color="amber" name="保单咨询助手" subtitle="解答保险条款与保障范围" calls="5,136" rate="91.2%" trend="up" />
            <AssistantRow initials="财" color="mint" name="财务流程助理" subtitle="发票归集与报销政策指引" calls="3,208" rate="88.6%" trend="down" />
          </div>
          <div className="panel-footnote"><span className="live-pulse" />数据更新于 2 分钟前</div>
        </div>

        <div className="panel run-panel">
          <div className="panel-heading">
            <div>
              <div className="section-kicker">从请求到结果</div>
              <h2>运行路径</h2>
            </div>
            <span className="run-period">最近 24 小时 <ChevronDown size={13} /></span>
          </div>
          <div className="run-flow">
            <div className="flow-node flow-source"><span className="flow-icon"><FileText size={15} /></span><span><strong>用户请求</strong><small>24,860 次进入</small></span><span className="flow-check"><Check size={12} /></span></div>
            <div className="flow-link"><span /></div>
            <div className="flow-node"><span className="flow-icon violet"><Bot size={15} /></span><span><strong>助理编排</strong><small>12 个助理处理中</small></span><span className="flow-check"><Check size={12} /></span></div>
            <div className="flow-link"><span /></div>
            <div className="flow-node"><span className="flow-icon gold"><BookOpen size={15} /></span><span><strong>知识与工具</strong><small>命中率 92.6%</small></span><span className="flow-check"><Check size={12} /></span></div>
            <div className="flow-link"><span /></div>
            <div className="flow-node flow-result"><span className="flow-icon green"><ShieldCheck size={15} /></span><span><strong>可靠地交付</strong><small>86.4% 自动解决</small></span><span className="flow-check"><Check size={12} /></span></div>
          </div>
          <div className="flow-caption"><span>每一步可追踪</span><span>权限 · 引用 · 质量</span></div>
        </div>
      </section>

      <section className="bottom-grid">
        <div className="panel activity-panel">
          <div className="panel-heading">
            <div><div className="section-kicker">平台动态</div><h2>最近发生</h2></div>
            <button className="text-action" type="button">查看记录 <ArrowRight size={14} /></button>
          </div>
          <div className="activity-list">
            <ActivityRow icon={<Check size={14} />} tone="green" title="客户服务团队已发布新版本" detail="版本 1.8 · Olivia Lin" time="10:42" />
            <ActivityRow icon={<BookOpen size={14} />} tone="gold" title="产品手册完成索引" detail="知识库 · 126 个片段" time="09:18" />
            <ActivityRow icon={<ShieldCheck size={14} />} tone="blue" title="审批策略已更新" detail="退款金额超过 ¥500 需人工复核" time="昨天" />
          </div>
        </div>
        <div className="panel quick-panel">
          <div className="section-kicker">继续构建</div>
          <h2>下一步，交给你</h2>
          <p>把企业知识、流程和模型连接成可管理的 AI 能力。</p>
          <div className="quick-actions">
            <Link to="/knowledge"><span className="quick-icon gold"><BookOpen size={16} /></span><span><strong>添加知识</strong><small>导入文档并建立索引</small></span><ArrowRight size={15} /></Link>
            <Link to="/workflows"><span className="quick-icon violet"><Workflow size={16} /></span><span><strong>编排流程</strong><small>把业务步骤连接起来</small></span><ArrowRight size={15} /></Link>
          </div>
        </div>
      </section>
      <footer className="page-footer"><span>BeeWork TAP · 企业 AI 工作台</span><span>开发预览 · 运行数据为示例</span></footer>
      {createOpen && <CreateModal title="创建助理" onClose={() => setCreateOpen(false)} />}
    </>
  )
}

export function MetricCard({
  label,
  value,
  delta,
  detail,
  icon,
  data,
}: {
  label: string
  value: string
  delta?: string
  detail?: string
  icon: React.ReactNode
  data?: number[]
}) {
  return (
    <article className="metric-card">
      <div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div>
      <div className="metric-value-row"><strong>{value}</strong>{delta && <span className="metric-delta"><ArrowDownRight size={13} />{delta}</span>}</div>
      <div className="metric-bottom">{data ? <span>较上月</span> : <span>{detail}</span>}{data && <div className="mini-chart" aria-hidden="true">{data.map((height, index) => <i key={index} style={{ height: `${height}%` }} />)}</div>}</div>
    </article>
  )
}

export function AssistantRow({
  initials,
  color,
  name,
  subtitle,
  calls,
  rate,
  trend,
}: {
  initials: string
  color: string
  name: string
  subtitle: string
  calls: string
  rate: string
  trend: 'up' | 'down'
}) {
  return (
    <div className="assistant-row">
      <span className={`assistant-avatar ${color}`}>{initials}</span>
      <span className="assistant-name"><strong>{name}</strong><small>{subtitle}</small></span>
      <span className="assistant-usage"><strong>{calls}</strong><small>会话</small></span>
      <span className="assistant-rate"><strong>{rate}</strong><small>解决率</small></span>
      <span className={`trend-mark ${trend}`}><ArrowDownRight size={14} /></span>
      <button className="row-arrow" type="button" aria-label={`查看${name}`}><ArrowRight size={15} /></button>
    </div>
  )
}

export function ActivityRow({
  icon,
  tone,
  title,
  detail,
  time,
}: {
  icon: React.ReactNode
  tone: string
  title: string
  detail: string
  time: string
}) {
  return (
    <div className="activity-row">
      <span className={`activity-icon ${tone}`}>{icon}</span>
      <span className="activity-copy"><strong>{title}</strong><small>{detail}</small></span>
      <time>{time}</time>
    </div>
  )
}
