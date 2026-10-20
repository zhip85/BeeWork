import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  Bell,
  BookOpen,
  Bot,
  Boxes,
  Check,
  ChevronDown,
  CircleHelp,
  Command,
  FileText,
  Hexagon,
  LayoutDashboard,
  LogOut,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
} from 'lucide-react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import './App.css'

const navigation = [
  { label: '工作台', to: '/', icon: LayoutDashboard, end: true },
  { label: '助理', to: '/assistants', icon: Bot },
  { label: '编排中心', to: '/workflows', icon: Workflow },
  { label: '知识库', to: '/knowledge', icon: BookOpen },
  { label: '能力目录', to: '/capabilities', icon: Boxes },
]

type TenantMembership = {
  id: string
  name: string
  slug: string
  role: string
}

type CurrentUser = {
  id: string
  email: string
  display_name: string
  is_platform_admin: boolean
  memberships: TenantMembership[]
}

type TenantMember = {
  id: string
  email: string
  display_name: string
  role: string
  is_active: boolean
}

type PlatformTenant = {
  id: string
  name: string
  slug: string
  status: string
}

async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  tenantId?: string,
): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (tenantId) headers.set('X-Tenant-ID', tenantId)
  if (options.method && !['GET', 'HEAD', 'OPTIONS'].includes(options.method.toUpperCase())) {
    const csrfToken = document.cookie
      .split('; ')
      .find((cookie) => cookie.startsWith('beework_csrf='))
      ?.split('=')
      .slice(1)
      .join('=')
    if (csrfToken) headers.set('X-CSRF-Token', decodeURIComponent(csrfToken))
  }

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: 'same-origin',
  })
  if (response.status === 204) return undefined as T
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = typeof payload.detail === 'string' ? payload.detail : '请求失败，请稍后重试'
    throw new Error(message)
  }
  return payload as T
}

function App() {
  const [apiStatus, setApiStatus] = useState<'checking' | 'online' | 'offline'>(
    'checking',
  )
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [sessionLoading, setSessionLoading] = useState(true)
  const [sessionError, setSessionError] = useState('')
  const [activeTenantId, setActiveTenantId] = useState(
    () => localStorage.getItem('beework.activeTenant') ?? '',
  )
  const location = useLocation()
  const activeTenant = useMemo(
    () => user?.memberships.find((tenant) => tenant.id === activeTenantId) ?? user?.memberships[0] ?? null,
    [activeTenantId, user],
  )
  const isTenantAdmin = activeTenant?.role === 'tenant_admin'
  const currentPage =
    location.pathname === '/members'
      ? '成员与权限'
      : location.pathname === '/platform/tenants'
      ? '企业租户'
      :
    navigation.find((item) =>
      item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
    )?.label ?? '工作台'

  useEffect(() => {
    let active = true
    fetch('/health/ready')
      .then((response) => {
        if (!response.ok) throw new Error('API is not ready')
        return response.json()
      })
      .then(() => active && setApiStatus('online'))
      .catch(() => active && setApiStatus('offline'))
    apiRequest<CurrentUser>('/api/v1/auth/me')
      .then((currentUser) => {
        if (!active) return
        setUser(currentUser)
        setSessionError('')
        const storedTenant = localStorage.getItem('beework.activeTenant')
        if (!storedTenant || !currentUser.memberships.some((item) => item.id === storedTenant)) {
          const firstTenant = currentUser.memberships[0]?.id ?? ''
          setActiveTenantId(firstTenant)
          if (firstTenant) localStorage.setItem('beework.activeTenant', firstTenant)
        }
      })
      .catch((error: Error) => {
        if (!active) return
        if (error.message !== '请先登录' && error.message !== '登录已过期，请重新登录') {
          setSessionError(error.message)
        }
      })
      .finally(() => active && setSessionLoading(false))
    return () => {
      active = false
    }
  }, [])

  async function handleLogin(email: string, password: string) {
    const currentUser = await apiRequest<CurrentUser>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
    setUser(currentUser)
    setSessionError('')
    const firstTenant = currentUser.memberships[0]?.id ?? ''
    setActiveTenantId(firstTenant)
    if (firstTenant) localStorage.setItem('beework.activeTenant', firstTenant)
  }

  async function handleLogout() {
    try {
      await apiRequest<void>('/api/v1/auth/logout', { method: 'POST' })
      setUser(null)
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : '退出登录失败')
    }
  }

  function handleTenantChange(tenantId: string) {
    setActiveTenantId(tenantId)
    localStorage.setItem('beework.activeTenant', tenantId)
  }

  if (sessionLoading) {
    return <div className="auth-loading"><span className="brand-mark"><Hexagon size={22} /><span /></span>正在连接 BeeWork 工作区</div>
  }

  if (!user) {
    return <LoginPage onLogin={handleLogin} error={sessionError} />
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="BeeWork 工作台">
          <span className="brand-mark">
            <Hexagon size={22} strokeWidth={2.2} />
            <span />
          </span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">TAP</span>
        </Link>

        <label className="workspace-switch">
          <span className="workspace-avatar">{activeTenant?.name.slice(0, 1) ?? 'A'}</span>
          <span className="workspace-copy">
            <strong>{activeTenant?.name ?? '未加入企业'}</strong>
            <small>{activeTenant ? roleLabel(activeTenant.role) : '请联系平台管理员'}</small>
          </span>
          {user.memberships.length > 1 ? (
            <select
              aria-label="切换企业工作区"
              value={activeTenant?.id ?? ''}
              onChange={(event) => handleTenantChange(event.target.value)}
            >
              {user.memberships.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}
            </select>
          ) : <ChevronDown size={15} />}
        </label>

        <div className="nav-caption">工作空间</div>
        <nav className="primary-nav" aria-label="主导航">
          {navigation.map(({ label, to, icon: Icon, end }) => (
            <NavLink
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              end={end}
              key={to}
              to={to}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{label}</span>
              {label === '助理' && <span className="nav-count">12</span>}
            </NavLink>
          ))}
        </nav>

        <div className="nav-caption nav-caption-lower">管理</div>
        <NavLink className="nav-link" to="/operations">
          <Activity size={18} strokeWidth={1.8} />
          <span>运营与审计</span>
        </NavLink>
        <NavLink className="nav-link" to="/settings">
          <Settings2 size={18} strokeWidth={1.8} />
          <span>设置</span>
        </NavLink>
        {isTenantAdmin && (
          <NavLink className="nav-link" to="/members">
            <ShieldCheck size={18} strokeWidth={1.8} />
            <span>成员与权限</span>
          </NavLink>
        )}
        {user.is_platform_admin && (
          <NavLink className="nav-link" to="/platform/tenants">
            <Boxes size={18} strokeWidth={1.8} />
            <span>平台租户</span>
          </NavLink>
        )}

        <div className="sidebar-spacer" />
        <div className="plan-card">
          <div className="plan-icon"><Sparkles size={16} /></div>
          <strong>让 AI 团队持续进化</strong>
          <span>探索你的企业能力图谱</span>
          <Link className="plan-link" to="/capabilities">
            查看能力目录 <ArrowRight size={14} />
          </Link>
        </div>
        <button className="profile-button" type="button" onClick={handleLogout} title="退出登录">
          <span className="profile-avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
          <span className="profile-copy"><strong>{user.display_name}</strong><small>{roleLabel(activeTenant?.role ?? '')}</small></span>
          <LogOut size={15} />
        </button>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>{activeTenant?.name ?? 'BeeWork'}</span><span className="crumb-slash">/</span><strong>{currentPage}</strong>
          </div>
          <div className="topbar-actions">
            <button className="search-button" type="button" aria-label="搜索">
              <Search size={16} /><span>搜索任何内容</span><kbd><Command size={11} /> K</kbd>
            </button>
            <span className={`service-status ${apiStatus}`} title={`API ${apiStatus}`}>
              <i />{apiStatus === 'online' ? '系统在线' : apiStatus === 'offline' ? 'API 未连接' : '正在连接'}
            </span>
            <button className="icon-button notification-button" type="button" aria-label="通知">
              <Bell size={18} /><i />
            </button>
            <button className="help-button" type="button"><CircleHelp size={17} />帮助</button>
          </div>
        </header>
        <div className="page-content">
          <Routes>
            <Route path="/" element={<Dashboard user={user} tenant={activeTenant} />} />
            {navigation.slice(1).map(({ label, to, icon: Icon }) => (
              <Route key={to} path={to} element={<SectionPlaceholder title={label} icon={<Icon size={22} />} />} />
            ))}
            <Route path="/operations" element={<SectionPlaceholder title="运营与审计" icon={<Activity size={22} />} />} />
            <Route path="/settings" element={<SectionPlaceholder title="设置" icon={<Settings2 size={22} />} />} />
            <Route path="/members" element={isTenantAdmin && activeTenant ? <MembersPage key={activeTenant.id} tenant={activeTenant} onMembershipChanged={async () => {
              const refreshed = await apiRequest<CurrentUser>('/api/v1/auth/me')
              setUser(refreshed)
            }} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="/platform/tenants" element={user.is_platform_admin ? <PlatformTenantsPage onTenantCreated={async (tenantId) => {
              const refreshed = await apiRequest<CurrentUser>('/api/v1/auth/me')
              setUser(refreshed)
              if (refreshed.memberships.some((tenant) => tenant.id === tenantId)) {
                handleTenantChange(tenantId)
              }
            }} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="*" element={<SectionPlaceholder title="工作台" icon={<LayoutDashboard size={22} />} />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}

function roleLabel(role: string) {
  const labels: Record<string, string> = {
    tenant_admin: '企业管理员',
    builder: '构建者',
    operator: '运营者',
    approver: '审批者',
    member: '使用者',
  }
  return labels[role] ?? role
}

function LoginPage({
  onLogin,
  error,
}: {
  onLogin: (email: string, password: string) => Promise<void>
  error: string
}) {
  const [email, setEmail] = useState(import.meta.env.DEV ? 'admin@beework.dev' : '')
  const [password, setPassword] = useState(import.meta.env.DEV ? 'Beework-Dev-2026!' : '')
  const [formError, setFormError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setFormError('')
    try {
      await onLogin(email, password)
    } catch (loginError) {
      setFormError(loginError instanceof Error ? loginError.message : '登录失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-screen">
      <section className="login-panel">
        <Link className="brand login-brand" to="/" aria-label="BeeWork">
          <span className="brand-mark"><Hexagon size={22} strokeWidth={2.2} /><span /></span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">TAP</span>
        </Link>
        <div className="login-eyebrow"><span className="eyebrow-dot" />企业 AI 工作空间</div>
        <h1>让工作，<br />从容一点。</h1>
        <p className="login-description">登录 BeeWork，管理你的企业 AI 助理与工作流程。</p>
        <form className="login-form" onSubmit={submit}>
          <label htmlFor="login-email">企业邮箱</label>
          <input
            autoComplete="username"
            id="login-email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
          <label htmlFor="login-password">密码</label>
          <input
            autoComplete="current-password"
            id="login-password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          {(formError || error) && <p className="login-error" role="alert">{formError || error}</p>}
          <button className="primary-button login-submit" disabled={submitting} type="submit">
            {submitting ? '正在登录…' : '登录工作空间'} <ArrowRight size={16} />
          </button>
        </form>
        {import.meta.env.DEV && (
          <div className="login-demo-note">
            <strong>本地开发账号</strong>
            <span>admin@beework.dev</span>
            <span>Beework-Dev-2026!</span>
          </div>
        )}
        <span className="login-footnote">BeeWork TAP · 企业 AI 平台</span>
      </section>
      <aside className="login-aside">
        <div className="login-aside-orbit orbit-one" />
        <div className="login-aside-orbit orbit-two" />
        <div className="login-aside-copy">
          <div className="login-aside-kicker">BUILD · RUN · GOVERN</div>
          <h2>让每一种<br />企业能力，<em>各就其位。</em></h2>
          <p>把知识、工具和 AI 团队，编织进值得信任的工作流程。</p>
          <div className="login-aside-bottom"><span>一处构建</span><i /><span>协同运行</span><i /><span>全程可治理</span></div>
        </div>
      </aside>
    </main>
  )
}

function MembersPage({
  tenant,
  onMembershipChanged,
}: {
  tenant: TenantMembership
  onMembershipChanged: () => Promise<void>
}) {
  const [members, setMembers] = useState<TenantMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatingMember, setUpdatingMember] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<{ tenant_id: string; members: TenantMember[] }>(
      '/api/v1/members',
      {},
      tenant.id,
    )
      .then((result) => active && setMembers(result.members))
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [tenant.id])

  async function updateRole(member: TenantMember, role: string) {
    setUpdatingMember(member.id)
    setError('')
    try {
      const updated = await apiRequest<TenantMember>(
        `/api/v1/members/${member.id}/role`,
        { method: 'PATCH', body: JSON.stringify({ role }) },
        tenant.id,
      )
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item))
      await onMembershipChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '角色更新失败')
    } finally {
      setUpdatingMember('')
    }
  }

  return (
    <section className="members-page">
      <div className="members-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />企业访问控制</div>
          <h1>成员与权限</h1>
          <p>管理谁可以进入 {tenant.name}，以及他们在工作区内可以执行的操作。</p>
        </div>
        <div className="members-count"><strong>{members.length}</strong><span>位成员</span></div>
      </div>
      <div className="members-note"><ShieldCheck size={17} /><span>角色只在当前企业工作区生效。平台管理员权限与企业角色分开管理。</span></div>
      <div className="panel members-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">工作区成员</div><h2>{tenant.name}</h2></div>
          <span className="tenant-role-chip">{roleLabel(tenant.role)}</span>
        </div>
        {error && <div className="members-error" role="alert">{error}</div>}
        {loading ? (
          <div className="members-empty">正在读取成员列表…</div>
        ) : members.length === 0 ? (
          <div className="members-empty">此工作区还没有成员。</div>
        ) : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>成员</th><th>角色</th><th>账号状态</th></tr></thead>
              <tbody>
                {members.map((member) => (
                  <tr key={member.id}>
                    <td>
                      <div className="member-cell">
                        <span className="member-avatar">{member.display_name.slice(0, 1)}</span>
                        <span><strong>{member.display_name}</strong><small>{member.email}</small></span>
                      </div>
                    </td>
                    <td>
                      {tenant.role === 'tenant_admin' ? (
                        <select
                          aria-label={`${member.display_name}的角色`}
                          className="role-select"
                          disabled={updatingMember === member.id}
                          onChange={(event) => updateRole(member, event.target.value)}
                          value={member.role}
                        >
                          <option value="tenant_admin">企业管理员</option>
                          <option value="builder">构建者</option>
                          <option value="operator">运营者</option>
                          <option value="approver">审批者</option>
                          <option value="member">使用者</option>
                        </select>
                      ) : <span className="role-text">{roleLabel(member.role)}</span>}
                    </td>
                    <td><span className={`member-status ${member.is_active ? 'active' : 'inactive'}`}><i />{member.is_active ? '正常' : '已停用'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}

function PlatformTenantsPage({
  onTenantCreated,
}: {
  onTenantCreated: (tenantId: string) => Promise<void>
}) {
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<PlatformTenant[]>('/api/v1/platform/tenants')
      .then((result) => active && setTenants(result))
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  async function createTenant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    setNotice('')
    try {
      const created = await apiRequest<TenantMembership>(
        '/api/v1/platform/tenants',
        { method: 'POST', body: JSON.stringify({ name, slug }) },
      )
      setTenants((current) => [
        { id: created.id, name: created.name, slug: created.slug, status: 'active' },
        ...current,
      ])
      setName('')
      setSlug('')
      setNotice(`已创建 ${created.name}，并将其加入你的工作区列表。`)
      await onTenantCreated(created.id)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建企业失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="members-page platform-page">
      <div className="members-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />平台管理</div>
          <h1>企业租户</h1>
          <p>管理 BeeWork 平台上的企业工作区。每个企业的数据与成员权限独立隔离。</p>
        </div>
        <div className="members-count"><strong>{tenants.length}</strong><span>家企业</span></div>
      </div>
      <div className="members-note"><ShieldCheck size={17} /><span>平台租户操作仅平台管理员可用。创建企业后，你将作为该企业的管理员加入工作区。</span></div>
      <div className="panel tenant-create-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">新增客户工作区</div><h2>创建企业</h2></div>
          <span className="tenant-role-chip">仅平台管理员</span>
        </div>
        <form className="tenant-create-form" onSubmit={createTenant}>
          <label>企业名称<input maxLength={160} minLength={2} onChange={(event) => setName(event.target.value)} placeholder="例如：Acme 科技" required value={name} /></label>
          <label>工作区标识<input maxLength={80} minLength={3} onChange={(event) => setSlug(event.target.value.toLowerCase())} pattern="[a-z0-9][a-z0-9-]{1,78}[a-z0-9]" placeholder="例如：acme-tech" required value={slug} /><small>小写字母、数字和连字符；创建后用于识别工作区。</small></label>
          <button className="primary-button" disabled={submitting} type="submit"><Plus size={16} />{submitting ? '正在创建…' : '创建企业'}</button>
        </form>
        {notice && <p className="tenant-success" role="status">{notice}</p>}
        {error && <p className="members-error" role="alert">{error}</p>}
      </div>
      <div className="panel tenants-list-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">平台范围</div><h2>所有企业</h2></div>
        </div>
        {loading ? <div className="members-empty">正在读取企业列表…</div> : tenants.length === 0 ? <div className="members-empty">还没有企业工作区。</div> : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>企业名称</th><th>工作区标识</th><th>状态</th></tr></thead>
              <tbody>{tenants.map((tenant) => <tr key={tenant.id}><td><strong className="tenant-name-cell">{tenant.name}</strong></td><td><code className="tenant-slug">{tenant.slug}</code></td><td><span className={`member-status ${tenant.status === 'active' ? 'active' : 'inactive'}`}><i />{tenant.status === 'active' ? '运行中' : '已停用'}</span></td></tr>)}</tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}

function Dashboard({ user, tenant }: { user: CurrentUser; tenant: TenantMembership | null }) {
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

function MetricCard({
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

function AssistantRow({
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

function ActivityRow({
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

function SectionPlaceholder({ title, icon }: { title: string; icon: React.ReactNode }) {
  const [modalOpen, setModalOpen] = useState(false)
  return (
    <section className="placeholder-page">
      <div className="placeholder-icon">{icon}</div>
      <div className="section-kicker">工作空间</div>
      <h1>{title}</h1>
      <p>前端框架已就绪，这个模块会在后续开发中接入真实 API。</p>
      <button className="primary-button" type="button" onClick={() => setModalOpen(true)}><Plus size={17} />创建{title === '助理' ? '助理' : title === '知识库' ? '知识库' : '新项目'}</button>
      {modalOpen && <CreateModal title={title === '助理' ? '创建助理' : title === '知识库' ? '创建知识库' : `创建${title}`} onClose={() => setModalOpen(false)} />}
    </section>
  )
}

function CreateModal({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="create-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={(event) => event.stopPropagation()}>
        <button className="modal-close" type="button" aria-label="关闭" onClick={onClose}><X size={18} /></button>
        <div className="modal-symbol"><Sparkles size={19} /></div>
        <div className="section-kicker">新建工作</div>
        <h2 id="modal-title">{title}</h2>
        <p>页面框架已准备好，配置表单和保存能力会在模块开发时接入。</p>
        <button className="primary-button" type="button" onClick={onClose}>知道了</button>
      </div>
    </div>
  )
}

export default App
