import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  Bell,
  BookOpen,
  Bot,
  Building2,
  Boxes,
  Check,
  ChevronDown,
  CircleHelp,
  Command,
  Download,
  FileText,
  Hexagon,
  LayoutDashboard,
  LogOut,
  Plus,
  Pencil,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  Workflow,
  X,
} from 'lucide-react'
import { Link, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import './App.css'

const PLAN_LABELS: Record<string, string> = {
  standard: '标准版',
  professional: '专业版',
  enterprise: '企业版',
}
const REGION_LABELS: Record<string, string> = {
  us_east: 'US-East',
  eu_west: 'EU-West',
  ap_singapore: 'AP-Singapore',
  cn_shanghai: 'CN-Shanghai',
}
const MODEL_TYPE_LABELS: Record<string, string> = {
  chat: '对话与推理',
  embedding: '向量化',
  rerank: '重排序',
  vision: '视觉',
  speech: '语音',
}

const navigation = [
  { label: '工作台', to: '/', icon: LayoutDashboard, end: true, permission: 'dashboard:view' },
  { label: '助理', to: '/assistants', icon: Bot, permission: 'assistants:view' },
  { label: '编排中心', to: '/workflows', icon: Workflow, permission: 'orchestration:view' },
  { label: '知识库', to: '/knowledge', icon: BookOpen, permission: 'knowledge:view' },
  { label: '能力目录', to: '/capabilities', icon: Boxes, permission: 'capabilities:view' },
]

type TenantMembership = {
  id: string
  name: string
  slug: string
  role: string
  custom_role_id: string | null
  custom_role_name: string | null
  organization_id: string | null
  permissions: string[]
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
  status: 'active' | 'suspended'
  organization_id: string
  organization_name: string
  custom_role_id: string | null
  custom_role_name: string | null
}

type MemberInvitation = {
  id: string
  email: string
  role: string
  custom_role_id: string | null
  custom_role_name: string | null
  organization_id: string
  organization_name: string
  status: 'pending' | 'accepted' | 'revoked' | 'expired'
  expires_at: string
}

type CreatedMemberInvitation = MemberInvitation & { invitation_url: string }

type PermissionModule = {
  key: string
  label: string
  actions: string[]
}

type PermissionCatalog = {
  modules: PermissionModule[]
  action_labels: Record<string, string>
}

type TenantRole = {
  key: string
  id: string | null
  name: string
  description: string
  scope: 'tenant' | 'organization'
  scope_organization_id: string | null
  permissions: Record<string, string[]>
  is_active: boolean
  is_builtin: boolean
  member_count: number
}

type RoleDraft = {
  id: string | null
  name: string
  description: string
  scope: 'tenant' | 'organization'
  scope_organization_id: string
  permissions: Record<string, string[]>
  is_active: boolean
}

type Organization = {
  id: string
  tenant_id: string
  parent_id: string | null
  name: string
  depth: number
  member_count: number
  child_count: number
  is_root: boolean
}

type PlatformTenant = {
  id: string
  name: string
  slug: string
  status: string
  seat_limit: number
  plan: string
  region: string
  member_count: number
  admin_email: string | null
  admin_name: string | null
}

type TenantModelEnvironment = {
  tenant_id: string
  environment: 'development' | 'testing' | 'production'
  plan: string
  region: string
  version: number
  updated_at: string | null
  models: Array<{
    id: string
    display_name: string
    provider: string
    model_type: string
    region: string
    input_price_per_million: string
  }>
  enabled_model_ids: string[]
  default_model_ids: Record<'chat' | 'embedding' | 'rerank', string | null>
  unavailable_enabled_model_ids: string[]
}

type PlatformModel = {
  id: string
  display_name: string
  provider: string
  model_type: string
  availability: string
  input_price_per_million: string
  output_price_per_million: string
  input_points_per_thousand: string
  output_points_per_thousand: string
  status: 'healthy' | 'canary' | 'disabled'
  provider_model_id: string
  endpoint_url: string
  credential_configured: boolean
  api_key_masked: string
  api_version: string | null
  region: string
  context_window: number | null
  max_output_tokens: number | null
  default_temperature: string | null
  rpm_limit: number | null
  fallback_model_id: string | null
  allowlist_tenant_ids: string[]
  created_at: string
  updated_at: string
}

type ModelDraft = {
  id: string | null
  display_name: string
  provider: string
  model_type: string
  availability: string
  input_price_per_million: string
  output_price_per_million: string
  input_points_per_thousand: string
  output_points_per_thousand: string
  status: PlatformModel['status']
  provider_model_id: string
  endpoint_url: string
  api_key: string
  api_version: string
  region: string
  context_window: string
  max_output_tokens: string
  default_temperature: string
  rpm_limit: string
  fallback_model_id: string
  allowlist_tenant_ids: string[]
}

type PlatformOverview = {
  tenant_count: number
  active_tenant_count: number
  suspended_tenant_count: number
  member_count: number
  active_member_count: number
  platform_admin_count: number
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
  const hasPermission = (permission: string) => Boolean(activeTenant?.permissions.includes(permission))
  const canViewSettings = hasPermission('settings:view')
  const canEditSettings = hasPermission('settings:edit')
  const canViewOperations = [
    'conversations:view',
    'handoff:view',
    'annotations:view',
    'evaluations:view',
    'traces:view',
    'approvals:view',
    'audit:view',
  ].some(hasPermission)
  const currentPage =
    location.pathname === '/members'
      ? '成员与权限'
      : location.pathname === '/audit'
      ? '审计日志'
      : location.pathname === '/platform/tenants'
      ? '企业租户'
      : location.pathname === '/settings'
      ? '模型与环境'
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
    if (location.pathname === '/invite') {
      return <InviteAcceptancePage onAccepted={(acceptedUser) => {
        setUser(acceptedUser)
        const firstTenant = acceptedUser.memberships[0]?.id ?? ''
        setActiveTenantId(firstTenant)
        if (firstTenant) localStorage.setItem('beework.activeTenant', firstTenant)
      }} />
    }
    return <LoginPage onLogin={handleLogin} error={sessionError} />
  }

  if (!user.is_platform_admin && !user.memberships.length) {
    return <NoTenantAccessPage user={user} onLogout={handleLogout} />
  }

  if (user.is_platform_admin && !user.memberships.length && !location.pathname.startsWith('/platform')) {
    return <Navigate to="/platform" replace />
  }

  if (user.is_platform_admin && location.pathname.startsWith('/platform')) {
    return <PlatformConsole user={user} onLogout={handleLogout} />
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
            <small>{activeTenant ? roleLabel(activeTenant.role, activeTenant.custom_role_name) : '请联系平台管理员'}</small>
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
          {navigation.filter((item) => hasPermission(item.permission)).map(({ label, to, icon: Icon, end }) => (
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
        {canViewOperations && (
          <NavLink className="nav-link" to="/operations">
            <Activity size={18} strokeWidth={1.8} />
            <span>运营与审计</span>
          </NavLink>
        )}
        {canViewSettings && (
          <NavLink className="nav-link" to="/settings">
            <Settings2 size={18} strokeWidth={1.8} />
            <span>设置</span>
          </NavLink>
        )}
        {hasPermission('audit:view') && (
          <NavLink className="nav-link" to="/audit">
            <FileText size={18} strokeWidth={1.8} />
            <span>审计日志</span>
          </NavLink>
        )}
        {isTenantAdmin && (
          <NavLink className="nav-link" to="/members">
            <ShieldCheck size={18} strokeWidth={1.8} />
            <span>成员与权限</span>
          </NavLink>
        )}
        {user.is_platform_admin && (
          <NavLink className="nav-link" to="/platform">
            <Boxes size={18} strokeWidth={1.8} />
            <span>平台运营</span>
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
          <span className="profile-copy"><strong>{user.display_name}</strong><small>{roleLabel(activeTenant?.role ?? '', activeTenant?.custom_role_name)}</small></span>
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
            <Route path="/" element={hasPermission('dashboard:view') ? <Dashboard user={user} tenant={activeTenant} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            {navigation.slice(1).map(({ label, to, icon: Icon, permission }) => (
              <Route key={to} path={to} element={hasPermission(permission) ? <SectionPlaceholder title={label} icon={<Icon size={22} />} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            ))}
            <Route path="/operations" element={canViewOperations ? <SectionPlaceholder title="运营与审计" icon={<Activity size={22} />} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="/audit" element={hasPermission('audit:view') && activeTenant ? <AuditLogPage key={activeTenant.id} tenantId={activeTenant.id} canExport={hasPermission('audit:export')} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings" element={canViewSettings && activeTenant ? <TenantModelSettingsPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={canEditSettings} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="/members" element={isTenantAdmin && activeTenant ? <MembersPage key={activeTenant.id} tenant={activeTenant} currentUserId={user.id} onMembershipChanged={async () => {
              const refreshed = await apiRequest<CurrentUser>('/api/v1/auth/me')
              setUser(refreshed)
            }} /> : <SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="/platform/*" element={<SectionPlaceholder title="无权访问" icon={<ShieldCheck size={22} />} />} />
            <Route path="*" element={<SectionPlaceholder title="工作台" icon={<LayoutDashboard size={22} />} />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}

function roleLabel(role: string, customRoleName?: string | null) {
  if (role === 'custom' && customRoleName) return customRoleName
  const labels: Record<string, string> = {
    tenant_admin: '企业管理员',
    builder: '构建者',
    operator: '运营者',
    viewer: '查看者',
    auditor: '审计员',
    approver: '审计员',
    member: '查看者',
  }
  return labels[role] ?? role
}

function InviteAcceptancePage({
  onAccepted,
}: {
  onAccepted: (user: CurrentUser) => void
}) {
  const location = useLocation()
  const token = new URLSearchParams(location.search).get('token') ?? ''
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  async function acceptInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const user = await apiRequest<CurrentUser>('/api/v1/members/invitations/accept', {
        method: 'POST',
        body: JSON.stringify({
          token,
          password,
          display_name: displayName.trim() || null,
        }),
      })
      onAccepted(user)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '接受邀请失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-screen">
      <section className="auth-card">
        <div className="eyebrow"><span className="eyebrow-dot" />企业工作区邀请</div>
        <h1>接受成员邀请</h1>
        <p>设置登录密码后即可进入企业工作区。已有 BeeWork 账号的成员请使用原账号密码。</p>
        {!token && <div className="members-error" role="alert">邀请链接缺少有效凭据，请向企业管理员索取新链接。</div>}
        {error && <div className="members-error" role="alert">{error}</div>}
        <form className="login-form" onSubmit={(event) => void acceptInvitation(event)}>
          <label>姓名<input autoComplete="name" maxLength={120} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
          <label>密码<input autoComplete="new-password" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} /><small>至少 12 位；已有账号请输入当前密码。</small></label>
          <button className="primary-button" disabled={!token || submitting} type="submit">{submitting ? '正在接受邀请…' : '接受邀请并进入工作区'}</button>
        </form>
      </section>
    </main>
  )
}

function LoginPage({
  onLogin,
  error,
}: {
  onLogin: (email: string, password: string) => Promise<void>
  error: string
}) {
  const [loginType, setLoginType] = useState<'tenant' | 'platform'>('tenant')
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
        <div className="login-eyebrow"><span className="eyebrow-dot" />{loginType === 'platform' ? 'BeeWork 平台运营后台' : '企业 AI 工作空间'}</div>
        <h1>{loginType === 'platform' ? <>管理平台，<br />服务企业。</> : <>让工作，<br />从容一点。</>}</h1>
        <p className="login-description">{loginType === 'platform' ? '平台管理员管理租户与平台运营，不属于任何企业工作区。' : '企业管理员和成员在此管理企业 AI 助理与工作流程。'}</p>
        <div className="login-type-switch" role="group" aria-label="选择登录入口">
          <button
            aria-pressed={loginType === 'tenant'}
            className={loginType === 'tenant' ? 'selected' : ''}
            onClick={() => {
              setLoginType('tenant')
              if (import.meta.env.DEV) {
                setEmail('admin@beework.dev')
                setPassword('Beework-Dev-2026!')
              }
            }}
            type="button"
          >企业工作区</button>
          <button
            aria-pressed={loginType === 'platform'}
            className={loginType === 'platform' ? 'selected' : ''}
            onClick={() => {
              setLoginType('platform')
              if (import.meta.env.DEV) {
                setEmail('platform@beework.dev')
                setPassword('BeeWork-Platform-2026!')
              }
            }}
            type="button"
          >SaaS 平台管理</button>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label htmlFor="login-email">{loginType === 'platform' ? '平台管理员邮箱' : '企业邮箱'}</label>
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
            {submitting ? '正在登录…' : loginType === 'platform' ? '进入平台后台' : '登录企业工作区'} <ArrowRight size={16} />
          </button>
        </form>
        {import.meta.env.DEV && (
          <div className="login-demo-note">
            <strong>{loginType === 'platform' ? '本地 SaaS 平台管理员' : '本地企业管理员'}</strong>
            <span>{loginType === 'platform' ? 'platform@beework.dev' : 'admin@beework.dev'}</span>
            <span>{loginType === 'platform' ? 'BeeWork-Platform-2026!' : 'Beework-Dev-2026!'}</span>
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

function NoTenantAccessPage({
  user,
  onLogout,
}: {
  user: CurrentUser
  onLogout: () => Promise<void>
}) {
  return (
    <main className="no-tenant-screen">
      <section className="no-tenant-card">
        <span className="platform-identity-icon"><Building2 size={19} /></span>
        <div className="section-kicker">企业工作区不可用</div>
        <h1>当前账号没有可用的企业</h1>
        <p>{user.email} 尚未加入运行中的企业工作区。请联系企业管理员；如果企业刚刚恢复服务，请重新登录。</p>
        <button className="primary-button" onClick={() => void onLogout()} type="button"><LogOut size={15} />退出登录</button>
      </section>
    </main>
  )
}

function MembersPage({
  tenant,
  currentUserId,
  onMembershipChanged,
}: {
  tenant: TenantMembership
  currentUserId: string
  onMembershipChanged: () => Promise<void>
}) {
  const [members, setMembers] = useState<TenantMember[]>([])
  const [invitations, setInvitations] = useState<MemberInvitation[]>([])
  const [seatLimit, setSeatLimit] = useState(0)
  const [seatsUsed, setSeatsUsed] = useState(0)
  const [inviteEmails, setInviteEmails] = useState('')
  const [inviteRole, setInviteRole] = useState('builder')
  const [inviteOrganizationId, setInviteOrganizationId] = useState('')
  const [inviting, setInviting] = useState(false)
  const [oneTimeLinks, setOneTimeLinks] = useState<Record<string, string>>({})
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [tenantRoles, setTenantRoles] = useState<TenantRole[]>([])
  const [permissionCatalog, setPermissionCatalog] = useState<PermissionCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatingMember, setUpdatingMember] = useState('')
  const [departmentName, setDepartmentName] = useState('')
  const [parentOrganizationId, setParentOrganizationId] = useState('')
  const [creatingOrganization, setCreatingOrganization] = useState(false)
  const [editingOrganizationId, setEditingOrganizationId] = useState('')
  const [editedOrganizationName, setEditedOrganizationName] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<{ tenant_id: string; members: TenantMember[]; invitations: MemberInvitation[]; seat_limit: number; seats_used: number }>(
        '/api/v1/members',
        {},
        tenant.id,
      ),
      apiRequest<{ tenant_id: string; organizations: Organization[] }>(
        '/api/v1/organizations',
        {},
        tenant.id,
      ),
      apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenant.id),
      apiRequest<PermissionCatalog>('/api/v1/roles/catalog', {}, tenant.id),
    ])
      .then(([memberResult, organizationResult, roleResult, catalog]) => {
        if (!active) return
        setMembers(memberResult.members)
        setInvitations(memberResult.invitations)
        setSeatLimit(memberResult.seat_limit)
        setSeatsUsed(memberResult.seats_used)
        setOrganizations(organizationResult.organizations)
        setTenantRoles(roleResult.roles)
        setPermissionCatalog(catalog)
        setParentOrganizationId(
          organizationResult.organizations.find((organization) => organization.is_root)?.id ?? '',
        )
        setInviteOrganizationId(
          organizationResult.organizations.find((organization) => organization.is_root)?.id ?? '',
        )
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [tenant.id])

  async function refreshMembers() {
    const result = await apiRequest<{
      members: TenantMember[]
      invitations: MemberInvitation[]
      seat_limit: number
      seats_used: number
    }>('/api/v1/members', {}, tenant.id)
    setMembers(result.members)
    setInvitations(result.invitations)
    setSeatLimit(result.seat_limit)
    setSeatsUsed(result.seats_used)
  }

  async function createInvitations(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setInviting(true)
    setError('')
    try {
      const emails = inviteEmails.split(/[,\n;]/).map((email) => email.trim()).filter(Boolean)
      const isCustom = inviteRole.startsWith('custom:')
      const result = await apiRequest<{
        invitations: CreatedMemberInvitation[]
        seat_limit: number
        seats_used: number
      }>('/api/v1/members/invitations', {
        method: 'POST',
        body: JSON.stringify({
          emails,
          role: isCustom ? 'custom' : inviteRole,
          custom_role_id: isCustom ? inviteRole.slice('custom:'.length) : null,
          organization_id: inviteOrganizationId,
        }),
      }, tenant.id)
      setInvitations((current) => [...result.invitations, ...current])
      setOneTimeLinks((current) => ({
        ...current,
        ...Object.fromEntries(result.invitations.map((item) => [item.id, item.invitation_url])),
      }))
      setSeatLimit(result.seat_limit)
      setSeatsUsed(result.seats_used)
      setInviteEmails('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建邀请失败')
    } finally {
      setInviting(false)
    }
  }

  async function resendInvitation(invitation: MemberInvitation) {
    setError('')
    try {
      const renewed = await apiRequest<CreatedMemberInvitation>(
        `/api/v1/members/invitations/${invitation.id}/resend`,
        { method: 'POST' },
        tenant.id,
      )
      setInvitations((current) => current.map((item) => item.id === renewed.id ? renewed : item))
      setOneTimeLinks((current) => ({ ...current, [renewed.id]: renewed.invitation_url }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '重新生成邀请链接失败')
    }
  }

  async function revokeInvitation(invitation: MemberInvitation) {
    if (!window.confirm(`撤销发送给 ${invitation.email} 的邀请？旧邀请链接将立即失效。`)) return
    setError('')
    try {
      await apiRequest<void>(
        `/api/v1/members/invitations/${invitation.id}`,
        { method: 'DELETE' },
        tenant.id,
      )
      setOneTimeLinks((current) => {
        const next = { ...current }
        delete next[invitation.id]
        return next
      })
      await refreshMembers()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '撤销邀请失败')
    }
  }

  async function copyInvitationLink(invitationId: string) {
    const link = oneTimeLinks[invitationId]
    if (!link) return
    try {
      await navigator.clipboard.writeText(link)
    } catch {
      setError('无法访问剪贴板，请检查浏览器权限后重试')
    }
  }

  async function changeMemberStatus(member: TenantMember) {
    const status = member.status === 'active' ? 'suspended' : 'active'
    const action = status === 'suspended' ? '暂停' : '恢复'
    if (!window.confirm(`${action}${member.display_name}？${status === 'suspended' ? '其当前登录会话将立即失效。' : '恢复操作需要有可用席位。'}`)) return
    setUpdatingMember(member.id)
    setError('')
    try {
      await apiRequest<TenantMember>(`/api/v1/members/${member.id}/status`, {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      }, tenant.id)
      if (member.id === currentUserId && status === 'suspended') {
        window.location.reload()
        return
      }
      await refreshMembers()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : `${action}成员失败`)
    } finally {
      setUpdatingMember('')
    }
  }

  async function removeMember(member: TenantMember) {
    if (!window.confirm(`移除 ${member.display_name}？该操作不能撤销；其企业成员关系将删除。`)) return
    setUpdatingMember(member.id)
    setError('')
    try {
      await apiRequest<void>(`/api/v1/members/${member.id}`, { method: 'DELETE' }, tenant.id)
      if (member.id === currentUserId) {
        window.location.reload()
        return
      }
      await refreshMembers()
      await onMembershipChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '移除成员失败')
    } finally {
      setUpdatingMember('')
    }
  }

  async function updateRole(member: TenantMember, selectedRole: string) {
    setUpdatingMember(member.id)
    setError('')
    try {
      const isCustom = selectedRole.startsWith('custom:')
      const role = isCustom ? 'custom' : selectedRole
      const updated = await apiRequest<TenantMember>(
        `/api/v1/members/${member.id}/role`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            role,
            custom_role_id: isCustom ? selectedRole.slice('custom:'.length) : null,
          }),
        },
        tenant.id,
      )
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item))
      const refreshedRoles = await apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenant.id)
      setTenantRoles(refreshedRoles.roles)
      await onMembershipChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '角色更新失败')
    } finally {
      setUpdatingMember('')
    }
  }

  async function createOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setCreatingOrganization(true)
    setError('')
    try {
      await apiRequest<Organization>(
        '/api/v1/organizations',
        {
          method: 'POST',
          body: JSON.stringify({
            name: departmentName,
            parent_id: parentOrganizationId || null,
          }),
        },
        tenant.id,
      )
      const refreshed = await apiRequest<{ tenant_id: string; organizations: Organization[] }>(
        '/api/v1/organizations',
        {},
        tenant.id,
      )
      setOrganizations(refreshed.organizations)
      setDepartmentName('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门创建失败')
    } finally {
      setCreatingOrganization(false)
    }
  }

  async function saveOrganizationName(organization: Organization) {
    setError('')
    try {
      const updated = await apiRequest<Organization>(
        `/api/v1/organizations/${organization.id}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ name: editedOrganizationName }),
        },
        tenant.id,
      )
      setOrganizations((current) => current.map((item) => item.id === updated.id ? updated : item))
      setEditingOrganizationId('')
      setEditedOrganizationName('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门名称更新失败')
    }
  }

  async function deleteOrganization(organization: Organization) {
    if (!window.confirm(`删除部门“${organization.name}”？此操作不可撤销。`)) return
    setError('')
    try {
      await apiRequest<void>(
        `/api/v1/organizations/${organization.id}`,
        { method: 'DELETE' },
        tenant.id,
      )
      setOrganizations((current) => current.filter((item) => item.id !== organization.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门删除失败')
    }
  }

  async function changeOrganization(member: TenantMember, organizationId: string) {
    setUpdatingMember(member.id)
    setError('')
    try {
      const updated = await apiRequest<TenantMember>(
        `/api/v1/members/${member.id}/organization`,
        {
          method: 'PATCH',
          body: JSON.stringify({ organization_id: organizationId }),
        },
        tenant.id,
      )
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item))
      setOrganizations((current) => current.map((item) => {
        const delta = item.id === updated.organization_id ? 1 : item.id === member.organization_id ? -1 : 0
        return { ...item, member_count: item.member_count + delta }
      }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '成员部门更新失败')
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
          <div><div className="section-kicker">成员席位</div><h2>已用 {seatsUsed} / {seatLimit} 个席位</h2></div>
          <span className="tenant-role-chip">启用成员与有效邀请计入席位</span>
        </div>
        <p className="organization-description">邀请链接有效 7 天。当前未配置邮件投递，请复制链接并通过安全渠道转发；链接仅在创建或重新生成时显示。</p>
        <form className="organization-create-form invitation-form" onSubmit={(event) => void createInvitations(event)}>
          <label>
            <span>邀请邮箱</span>
            <textarea
              aria-label="邀请邮箱"
              onChange={(event) => setInviteEmails(event.target.value)}
              placeholder="name@company.com，可输入多个邮箱，以逗号或换行分隔"
              required
              rows={3}
              value={inviteEmails}
            />
          </label>
          <label>
            <span>角色</span>
            <select onChange={(event) => setInviteRole(event.target.value)} value={inviteRole}>
              {tenantRoles.filter((role) => role.is_builtin || role.is_active).map((role) => (
                <option key={role.key} value={role.key}>{role.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span>所属部门</span>
            <select onChange={(event) => setInviteOrganizationId(event.target.value)} required value={inviteOrganizationId}>
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>{'　'.repeat(organization.depth)}{organization.name}</option>
              ))}
            </select>
          </label>
          <button className="primary-button" disabled={inviting || !inviteEmails.trim() || !inviteOrganizationId} type="submit">
            <Plus size={15} />{inviting ? '正在创建邀请…' : '创建邀请链接'}
          </button>
        </form>
        {invitations.length > 0 && (
          <div className="invitation-list">
            {invitations.map((invitation) => {
              const invitationStatus = invitation.status
              const statusLabels: Record<string, string> = {
                pending: '已邀请',
                accepted: '已接受',
                expired: '已过期',
                revoked: '已撤销',
              }
              return (
                <div className="invitation-row" key={invitation.id}>
                  <span className="invitation-copy"><strong>{invitation.email}</strong><small>{roleLabel(invitation.role, invitation.custom_role_name)} · {invitation.organization_name}</small></span>
                  <span className={`member-status ${invitationStatus === 'pending' ? 'active' : 'inactive'}`}><i />{statusLabels[invitationStatus]}</span>
                  {oneTimeLinks[invitation.id] && invitationStatus === 'pending' && (
                    <button
                      className="text-action"
                      onClick={() => void copyInvitationLink(invitation.id)}
                      type="button"
                    >复制邀请链接</button>
                  )}
                  {(invitationStatus === 'pending' || invitationStatus === 'expired') && (
                    <button className="text-action" onClick={() => void resendInvitation(invitation)} type="button">重新生成</button>
                  )}
                  {invitationStatus === 'pending' && (
                    <button className="text-action danger-action" onClick={() => void revokeInvitation(invitation)} type="button">撤销</button>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
      {permissionCatalog && (
        <RoleManagementPanel
          catalog={permissionCatalog}
          organizations={organizations}
          roles={tenantRoles}
          tenantId={tenant.id}
          onRolesChanged={setTenantRoles}
        />
      )}
      <div className="panel members-panel organization-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">组织架构</div><h2>部门与成员范围</h2></div>
          <span className="tenant-role-chip">{organizations.length} 个部门</span>
        </div>
        <p className="organization-description">部门用于成员分组与数据范围授权。根组织受保护；有成员或子部门的部门不能删除。</p>
        {error && <div className="members-error" role="alert">{error}</div>}
        {!loading && organizations.length > 0 && (
          <div className="organization-list" role="list" aria-label="部门列表">
            {organizations.map((organization) => (
              <div className="organization-row" key={organization.id} role="listitem">
                <span className={`organization-marker${organization.is_root ? ' root' : ''}`} style={{ marginLeft: organization.depth * 20 }}>
                  <Building2 size={15} />
                </span>
                {editingOrganizationId === organization.id ? (
                  <form
                    className="organization-rename"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void saveOrganizationName(organization)
                    }}
                  >
                    <input
                      aria-label="部门名称"
                      autoFocus
                      maxLength={80}
                      onChange={(event) => setEditedOrganizationName(event.target.value)}
                      required
                      value={editedOrganizationName}
                    />
                    <button className="text-action" type="submit">保存</button>
                    <button className="text-action" onClick={() => setEditingOrganizationId('')} type="button">取消</button>
                  </form>
                ) : (
                  <>
                    <span className="organization-copy">
                      <strong>{organization.name}</strong>
                      <small>{organization.is_root ? '根组织' : `${organization.member_count} 位成员 · ${organization.child_count} 个子部门`}</small>
                    </span>
                    {!organization.is_root && (
                      <span className="organization-actions">
                        <button
                          aria-label={`重命名${organization.name}`}
                          className="icon-button"
                          onClick={() => {
                            setEditingOrganizationId(organization.id)
                            setEditedOrganizationName(organization.name)
                          }}
                          type="button"
                        ><Pencil size={14} /></button>
                        <button
                          aria-label={`删除${organization.name}`}
                          className="icon-button organization-delete"
                          onClick={() => void deleteOrganization(organization)}
                          type="button"
                        ><Trash2 size={14} /></button>
                      </span>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        )}
        {!loading && organizations.length === 0 && !error && (
          <div className="members-empty">尚未设置组织架构。请刷新页面重试，或联系平台管理员检查租户初始化。</div>
        )}
        <form className="organization-create-form" onSubmit={createOrganization}>
          <label>
            <span>新部门名称</span>
            <input
              maxLength={80}
              onChange={(event) => setDepartmentName(event.target.value)}
              placeholder="例如：客户成功部"
              required
              value={departmentName}
            />
          </label>
          <label>
            <span>上级部门</span>
            <select
              onChange={(event) => setParentOrganizationId(event.target.value)}
              required
              value={parentOrganizationId}
            >
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {'　'.repeat(organization.depth)}{organization.name}
                </option>
              ))}
            </select>
          </label>
          <button className="primary-button" disabled={creatingOrganization || !departmentName.trim()} type="submit">
            <Plus size={15} />{creatingOrganization ? '正在创建…' : '新增部门'}
          </button>
        </form>
      </div>
      <div className="panel members-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">工作区成员</div><h2>{tenant.name}</h2></div>
          <span className="tenant-role-chip">{roleLabel(tenant.role, tenant.custom_role_name)}</span>
        </div>
        {loading ? (
          <div className="members-empty">正在读取成员列表…</div>
        ) : members.length === 0 ? (
          <div className="members-empty">此工作区还没有成员。</div>
        ) : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>成员</th><th>所属部门</th><th>角色</th><th>成员状态</th><th>操作</th></tr></thead>
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
                      <select
                        aria-label={`${member.display_name}的所属部门`}
                        className="role-select"
                        disabled={updatingMember === member.id || member.status !== 'active'}
                        onChange={(event) => void changeOrganization(member, event.target.value)}
                        value={member.organization_id}
                      >
                        {organizations.map((organization) => (
                          <option key={organization.id} value={organization.id}>
                            {'　'.repeat(organization.depth)}{organization.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {tenant.role === 'tenant_admin' ? (
                        <select
                          aria-label={`${member.display_name}的角色`}
                          className="role-select"
                          disabled={updatingMember === member.id || member.id === currentUserId || member.status !== 'active'}
                          onChange={(event) => void updateRole(member, event.target.value)}
                          value={member.role === 'custom' ? `custom:${member.custom_role_id}` : member.role}
                        >
                          {tenantRoles.filter((role) => role.is_builtin || role.is_active).map((role) => (
                            <option key={role.key} value={role.key}>
                              {role.name}
                            </option>
                          ))}
                        </select>
                      ) : <span className="role-text">{roleLabel(member.role, member.custom_role_name)}</span>}
                    </td>
                    <td><span className={`member-status ${member.status === 'active' ? 'active' : 'inactive'}`}><i />{member.status === 'active' ? '启用' : '已暂停'}</span></td>
                    <td className="member-actions-cell">
                      <button
                        className="text-action"
                        disabled={updatingMember === member.id}
                        onClick={() => void changeMemberStatus(member)}
                        type="button"
                      >{member.status === 'active' ? '暂停' : '恢复'}</button>
                      <button
                        className="text-action danger-action"
                        disabled={updatingMember === member.id}
                        onClick={() => void removeMember(member)}
                        type="button"
                      >移除</button>
                    </td>
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

function RoleManagementPanel({
  catalog,
  organizations,
  roles,
  tenantId,
  onRolesChanged,
}: {
  catalog: PermissionCatalog
  organizations: Organization[]
  roles: TenantRole[]
  tenantId: string
  onRolesChanged: (roles: TenantRole[]) => void
}) {
  const [draft, setDraft] = useState<RoleDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  function startNewRole(source?: TenantRole) {
    setError('')
    setDraft({
      id: null,
      name: source ? `${source.name}自定义` : '',
      description: source ? `基于「${source.name}」创建` : '',
      scope: 'tenant',
      scope_organization_id: '',
      permissions: source
        ? Object.fromEntries(
            Object.entries(source.permissions).map(([module, actions]) => [module, [...actions]]),
          )
        : {},
      is_active: true,
    })
  }

  async function refreshRoles() {
    const result = await apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenantId)
    onRolesChanged(result.roles)
  }

  async function saveRole(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft) return
    setSaving(true)
    setError('')
    try {
      const payload = {
        name: draft.name,
        description: draft.description,
        scope: draft.scope,
        scope_organization_id: draft.scope === 'organization' ? draft.scope_organization_id : null,
        permissions: draft.permissions,
        is_active: draft.is_active,
      }
      await apiRequest<TenantRole>(
        draft.id ? `/api/v1/roles/${draft.id}` : '/api/v1/roles',
        {
          method: draft.id ? 'PUT' : 'POST',
          body: JSON.stringify(payload),
        },
        tenantId,
      )
      await refreshRoles()
      setDraft(null)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存角色失败')
    } finally {
      setSaving(false)
    }
  }

  async function deleteRole(role: TenantRole) {
    if (!role.id || !window.confirm(`删除角色“${role.name}”？此操作不可撤销。`)) return
    setError('')
    try {
      await apiRequest<void>(`/api/v1/roles/${role.id}`, { method: 'DELETE' }, tenantId)
      await refreshRoles()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '删除角色失败')
    }
  }

  function togglePermission(module: string, action: string, enabled: boolean) {
    setDraft((current) => {
      if (!current) return current
      const actions = new Set(current.permissions[module] ?? [])
      if (enabled) actions.add(action)
      else actions.delete(action)
      const permissions = { ...current.permissions }
      if (actions.size) permissions[module] = [...actions]
      else delete permissions[module]
      return { ...current, permissions }
    })
  }

  return (
    <div className="panel role-management-panel">
      <div className="members-table-heading role-panel-heading">
        <div><div className="section-kicker">访问策略</div><h2>角色与权限</h2></div>
        <button className="primary-button" onClick={() => startNewRole()} type="button">
          <Plus size={15} />新建自定义角色
        </button>
      </div>
      <p className="organization-description">内置角色不可修改；自定义角色可按模块配置操作权限，并限定在租户或指定部门范围内。</p>
      {error && <div className="members-error" role="alert">{error}</div>}
      <div className="role-card-grid">
        {roles.map((role) => (
          <article className={`role-card${role.is_active ? '' : ' is-inactive'}`} key={role.key}>
            <div className="role-card-heading">
              <div><strong>{role.name}</strong><small>{role.is_builtin ? '系统内置' : role.scope === 'tenant' ? '全企业范围' : '部门范围'}</small></div>
              {!role.is_active && <span className="role-inactive-label">已停用</span>}
            </div>
            <p>{role.description || '未填写角色说明'}</p>
            <div className="role-card-footer">
              <span>{role.member_count} 位成员</span>
              {role.is_builtin ? (
                <button className="text-action" onClick={() => startNewRole(role)} type="button">以此角色创建</button>
              ) : (
                <span className="role-card-actions">
                  <button
                    className="text-action"
                    onClick={() => {
                      setError('')
                      setDraft({
                        id: role.id,
                        name: role.name,
                        description: role.description,
                        scope: role.scope,
                        scope_organization_id: role.scope_organization_id ?? '',
                        permissions: Object.fromEntries(
                          Object.entries(role.permissions).map(([module, actions]) => [module, [...actions]]),
                        ),
                        is_active: role.is_active,
                      })
                    }}
                    type="button"
                  >编辑</button>
                  <button
                    className="text-action danger-text-action"
                    disabled={role.member_count > 0}
                    onClick={() => void deleteRole(role)}
                    type="button"
                  >删除</button>
                </span>
              )}
            </div>
          </article>
        ))}
      </div>
      {draft && (
        <form className="role-editor" onSubmit={(event) => void saveRole(event)}>
          <div className="role-editor-heading">
            <div><div className="section-kicker">{draft.id ? '编辑自定义角色' : '新建自定义角色'}</div><h3>定义访问范围与操作权限</h3></div>
            <button aria-label="关闭角色编辑器" className="icon-button" onClick={() => setDraft(null)} type="button"><X size={17} /></button>
          </div>
          <div className="role-form-fields">
            <label><span>角色名称</span><input maxLength={80} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} /></label>
            <label><span>说明</span><input maxLength={500} onChange={(event) => setDraft({ ...draft, description: event.target.value })} value={draft.description} /></label>
            <label>
              <span>数据范围</span>
              <select
                onChange={(event) => setDraft({
                  ...draft,
                  scope: event.target.value as RoleDraft['scope'],
                  scope_organization_id: event.target.value === 'tenant' ? '' : (organizations[0]?.id ?? ''),
                })}
                value={draft.scope}
              >
                <option value="tenant">全企业</option>
                <option value="organization">指定部门及下级</option>
              </select>
            </label>
            {draft.scope === 'organization' && (
              <label>
                <span>授权部门</span>
                <select onChange={(event) => setDraft({ ...draft, scope_organization_id: event.target.value })} required value={draft.scope_organization_id}>
                  {organizations.map((organization) => (
                    <option key={organization.id} value={organization.id}>
                      {'　'.repeat(organization.depth)}{organization.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="permission-matrix-wrap">
            <table className="permission-matrix">
              <thead><tr><th>功能模块</th>{['view', 'edit', 'publish', 'delete', 'test', 'intervene', 'export', 'approve'].map((action) => (
                <th key={action}>{catalog.action_labels[action] ?? action}</th>
              ))}</tr></thead>
              <tbody>{catalog.modules.map((module) => (
                <tr key={module.key}>
                  <th>{module.label}</th>
                  {['view', 'edit', 'publish', 'delete', 'test', 'intervene', 'export', 'approve'].map((action) => (
                    <td key={action}>
                      {module.actions.includes(action) && (
                        <input
                          aria-label={`${module.label}${catalog.action_labels[action] ?? action}`}
                          checked={(draft.permissions[module.key] ?? []).includes(action)}
                          onChange={(event) => togglePermission(module.key, action, event.target.checked)}
                          type="checkbox"
                        />
                      )}
                    </td>
                  ))}
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="role-editor-footer">
            {draft.id && <label className="role-active-toggle"><input checked={draft.is_active} onChange={(event) => setDraft({ ...draft, is_active: event.target.checked })} type="checkbox" />角色启用</label>}
            <div>
              <button className="text-action" onClick={() => setDraft(null)} type="button">取消</button>
              <button className="primary-button" disabled={saving || !draft.name.trim()} type="submit">{saving ? '正在保存…' : '保存角色'}</button>
            </div>
          </div>
        </form>
      )}
    </div>
  )
}

function PlatformConsole({
  user,
  onLogout,
}: {
  user: CurrentUser
  onLogout: () => Promise<void>
}) {
  const location = useLocation()
  const isTenantPage = location.pathname === '/platform/tenants'
  const isAuditPage = location.pathname === '/platform/audit'
  const isModelPage = location.pathname === '/platform/models'

  return (
    <div className="platform-shell">
      <aside className="platform-sidebar">
        <Link className="brand" to="/platform" aria-label="BeeWork 平台总览">
          <span className="brand-mark"><Hexagon size={22} strokeWidth={2.2} /><span /></span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">PLATFORM</span>
        </Link>
        <div className="platform-identity">
          <span className="platform-identity-icon"><ShieldCheck size={18} /></span>
          <span><strong>SaaS 平台</strong><small>平台管理员控制台</small></span>
        </div>
        <div className="nav-caption">概览</div>
        <nav className="primary-nav" aria-label="平台导航">
          <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} end to="/platform">
            <LayoutDashboard size={18} strokeWidth={1.8} /><span>平台总览</span>
          </NavLink>
        </nav>
        <div className="nav-caption nav-caption-lower">运营数据</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/tenants">
          <Building2 size={18} strokeWidth={1.8} /><span>租户管理</span>
        </NavLink>
        <div className="nav-caption">平台定义</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/models">
          <Boxes size={18} strokeWidth={1.8} /><span>模型目录</span>
        </NavLink>
        <div className="nav-caption">审计</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/audit">
          <FileText size={18} strokeWidth={1.8} /><span>操作审计</span>
        </NavLink>
        <div className="platform-sidebar-note">
          <div className="section-kicker">权限边界</div>
          <p>平台账号负责平台与租户生命周期，不自动获得企业工作区权限。</p>
        </div>
        <div className="sidebar-spacer" />
        <button className="profile-button" onClick={() => void onLogout()} title="退出平台管理" type="button">
          <span className="profile-avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
          <span className="profile-copy"><strong>{user.display_name}</strong><small>SaaS 平台管理员</small></span>
          <LogOut size={15} />
        </button>
      </aside>
      <main className="main-area platform-main">
        <header className="topbar">
          <div className="breadcrumbs"><span>BeeWork 平台</span><span className="crumb-slash">/</span><strong>{isTenantPage ? '租户管理' : isAuditPage ? '操作审计' : isModelPage ? '模型目录' : '平台总览'}</strong></div>
          <div className="topbar-actions">
            <span className="platform-admin-badge"><i />平台管理员</span>
            {user.memberships.length > 0 && <Link className="platform-tenant-link" to="/">进入企业工作区</Link>}
          </div>
        </header>
        <div className="page-content platform-content">
          {isAuditPage
            ? <AuditLogPage key="platform-audit" canExport platformMode />
            : isModelPage
              ? <PlatformModelsPage />
              : isTenantPage
                ? <PlatformTenantsPage />
                : <PlatformOverviewPage />}
        </div>
      </main>
    </div>
  )
}

function PlatformOverviewPage() {
  const [overview, setOverview] = useState<PlatformOverview | null>(null)
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<PlatformOverview>('/api/v1/platform/overview'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
    ])
      .then(([summary, tenantList]) => {
        if (!active) return
        setOverview(summary)
        setTenants(tenantList.slice(0, 5))
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
                  <span className={`member-status ${tenant.status === 'active' ? 'active' : 'inactive'}`}><i />{tenant.status === 'active' ? '运行中' : '已停用'}</span>
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
      <footer className="page-footer"><span>BeeWork SaaS · 平台运营后台</span><span>{loading ? '正在同步数据' : '数据来自当前平台数据库'}</span></footer>
    </section>
  )
}

function PlatformMetric({
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

function PlatformTenantsPage() {
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [adminName, setAdminName] = useState('')
  const [adminEmail, setAdminEmail] = useState('')
  const [adminPassword, setAdminPassword] = useState('')
  const [seatLimit, setSeatLimit] = useState(10)
  const [plan, setPlan] = useState('standard')
  const [region, setRegion] = useState('ap_singapore')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [updatingTenant, setUpdatingTenant] = useState('')
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
      const created = await apiRequest<PlatformTenant>(
        '/api/v1/platform/tenants',
        {
          method: 'POST',
          body: JSON.stringify({
            name,
            slug,
            admin_name: adminName,
            admin_email: adminEmail,
            admin_password: adminPassword,
            seat_limit: seatLimit,
            plan,
            region,
          }),
        },
      )
      setTenants((current) => [created, ...current])
      setName('')
      setSlug('')
      setAdminName('')
      setAdminEmail('')
      setAdminPassword('')
      setSeatLimit(10)
      setPlan('standard')
      setRegion('ap_singapore')
      setNotice(`已创建 ${created.name}。首位企业管理员为 ${created.admin_email}；平台账号没有加入该企业。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建企业失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function changeStatus(tenant: PlatformTenant) {
    const nextStatus = tenant.status === 'active' ? 'suspended' : 'active'
    const action = nextStatus === 'suspended' ? '停用' : '恢复'
    if (!window.confirm(`${action}“${tenant.name}”？${nextStatus === 'suspended' ? '该企业成员将无法进入工作区。' : '该企业成员将可以重新登录。'}`)) return
    setUpdatingTenant(tenant.id)
    setError('')
    setNotice('')
    try {
      const updated = await apiRequest<PlatformTenant>(
        `/api/v1/platform/tenants/${tenant.id}/status`,
        { method: 'PATCH', body: JSON.stringify({ status: nextStatus }) },
      )
      setTenants((current) => current.map((item) => item.id === updated.id ? { ...item, ...updated } : item))
      setNotice(`${tenant.name}已${action}。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : `${action}企业失败`)
    } finally {
      setUpdatingTenant('')
    }
  }

  return (
    <section className="platform-tenants-page">
      <div className="platform-page-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />运营数据 · 租户</div>
          <h1>租户管理</h1>
          <p>开通企业工作区、设置首位企业管理员，并管理租户服务状态。</p>
        </div>
        <div className="members-count"><strong>{tenants.length}</strong><span>家企业</span></div>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <div className="panel tenant-create-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">租户开通</div><h2>创建企业工作区</h2></div>
          <span className="tenant-role-chip">不授予平台账号企业权限</span>
        </div>
        <p className="organization-description">初始密码由平台管理员设置，创建后不会再次显示；请先记录并通过安全渠道交付。</p>
        <form className="tenant-create-form" onSubmit={createTenant}>
          <label>企业名称<input maxLength={160} minLength={2} onChange={(event) => setName(event.target.value)} placeholder="例如：Acme 科技" required value={name} /></label>
          <label>工作区标识<input maxLength={80} minLength={3} onChange={(event) => setSlug(event.target.value.toLowerCase())} pattern="[a-z0-9][a-z0-9-]{1,78}[a-z0-9]" placeholder="例如：acme-tech" required value={slug} /><small>小写字母、数字和连字符；创建后用于识别工作区。</small></label>
          <label>企业管理员姓名<input maxLength={120} onChange={(event) => setAdminName(event.target.value)} placeholder="例如：Alex Chen" required value={adminName} /></label>
          <label>企业管理员邮箱<input autoComplete="off" onChange={(event) => setAdminEmail(event.target.value)} placeholder="admin@company.com" required type="email" value={adminEmail} /></label>
          <label>初始登录密码<input autoComplete="new-password" minLength={12} onChange={(event) => setAdminPassword(event.target.value)} required type="password" value={adminPassword} /><small>至少 12 位，请通过安全渠道交付给企业管理员。</small></label>
          <label>初始席位上限<input min={1} max={100000} onChange={(event) => setSeatLimit(Number(event.target.value))} required type="number" value={seatLimit} /><small>当前由平台按租户配置；后续套餐权益将接管此上限。</small></label>
          <label>套餐<select onChange={(event) => setPlan(event.target.value)} value={plan}><option value="standard">标准版</option><option value="professional">专业版</option><option value="enterprise">企业版</option></select></label>
          <label>数据区域<select onChange={(event) => setRegion(event.target.value)} value={region}><option value="us_east">US-East</option><option value="eu_west">EU-West</option><option value="ap_singapore">AP-Singapore</option><option value="cn_shanghai">CN-Shanghai</option></select><small>模型目录仅显示本区域可用模型。</small></label>
          <button className="primary-button tenant-provision-submit" disabled={submitting} type="submit"><Plus size={16} />{submitting ? '正在创建…' : '创建企业并开通管理员'}</button>
        </form>
      </div>
      <div className="panel tenants-list-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">租户生命周期</div><h2>所有企业</h2></div>
        </div>
        {loading ? <div className="members-empty">正在读取企业列表…</div> : tenants.length === 0 ? <div className="members-empty">还没有企业工作区。</div> : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>企业</th><th>首位企业管理员</th><th>套餐 / 区域</th><th>席位上限</th><th>成员数</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{tenants.map((tenant) => (
                <tr key={tenant.id}>
                  <td><strong className="tenant-name-cell">{tenant.name}</strong><small className="platform-tenant-slug">{tenant.slug}</small></td>
                  <td>{tenant.admin_name ? <span className="platform-admin-cell"><strong>{tenant.admin_name}</strong><small>{tenant.admin_email}</small></span> : <span className="role-text">未设置</span>}</td>
                  <td><span className="platform-admin-cell"><strong>{PLAN_LABELS[tenant.plan] ?? tenant.plan}</strong><small>{REGION_LABELS[tenant.region] ?? tenant.region}</small></span></td>
                  <td>{tenant.seat_limit}</td>
                  <td>{tenant.member_count}</td>
                  <td><span className={`member-status ${tenant.status === 'active' ? 'active' : 'inactive'}`}><i />{tenant.status === 'active' ? '运行中' : '已停用'}</span></td>
                  <td><button className={`tenant-status-action${tenant.status === 'active' ? ' suspend' : ''}`} disabled={updatingTenant === tenant.id} onClick={() => void changeStatus(tenant)} type="button">{updatingTenant === tenant.id ? '处理中…' : tenant.status === 'active' ? '停用租户' : '恢复租户'}</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}

const MODEL_PROVIDERS: Record<string, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  azure_openai: 'Azure OpenAI',
  alibaba_qwen: '阿里云 Qwen',
  private: '私有部署',
}

const MODEL_TYPES: Record<string, string> = {
  chat: '对话',
  embedding: '向量',
  rerank: '重排',
  vision: '视觉',
  speech: '语音',
}

const MODEL_AVAILABILITY: Record<string, string> = {
  all_plans: '全部套餐',
  professional_plus: '专业版及以上',
  enterprise: '仅企业版',
  allowlist: '白名单租户',
}

const MODEL_REGIONS: Record<string, string> = {
  us_east: '美国东部',
  eu_west: '欧洲西部',
  ap_singapore: '亚太新加坡',
  cn_shanghai: '中国上海',
}

function emptyModelDraft(): ModelDraft {
  return {
    id: null,
    display_name: '',
    provider: 'openai',
    model_type: 'chat',
    availability: 'all_plans',
    input_price_per_million: '0',
    output_price_per_million: '0',
    input_points_per_thousand: '0',
    output_points_per_thousand: '0',
    status: 'healthy',
    provider_model_id: '',
    endpoint_url: '',
    api_key: '',
    api_version: '',
    region: 'us_east',
    context_window: '',
    max_output_tokens: '',
    default_temperature: '',
    rpm_limit: '',
    fallback_model_id: '',
    allowlist_tenant_ids: [],
  }
}

function PlatformModelsPage() {
  const [models, setModels] = useState<PlatformModel[]>([])
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [draft, setDraft] = useState<ModelDraft | null>(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function refreshModels() {
    const [modelList, tenantList] = await Promise.all([
      apiRequest<{ models: PlatformModel[] }>('/api/v1/platform/models'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
    ])
    setModels(modelList.models)
    setTenants(tenantList)
  }

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<{ models: PlatformModel[] }>('/api/v1/platform/models'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
    ])
      .then(([modelList, tenantList]) => {
        if (!active) return
        setModels(modelList.models)
        setTenants(tenantList)
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  function editModel(model: PlatformModel) {
    setError('')
    setNotice('')
    setDraft({
      id: model.id,
      display_name: model.display_name,
      provider: model.provider,
      model_type: model.model_type,
      availability: model.availability,
      input_price_per_million: model.input_price_per_million,
      output_price_per_million: model.output_price_per_million,
      input_points_per_thousand: model.input_points_per_thousand,
      output_points_per_thousand: model.output_points_per_thousand,
      status: model.status,
      provider_model_id: model.provider_model_id,
      endpoint_url: model.endpoint_url,
      api_key: '',
      api_version: model.api_version ?? '',
      region: model.region,
      context_window: model.context_window?.toString() ?? '',
      max_output_tokens: model.max_output_tokens?.toString() ?? '',
      default_temperature: model.default_temperature ?? '',
      rpm_limit: model.rpm_limit?.toString() ?? '',
      fallback_model_id: model.fallback_model_id ?? '',
      allowlist_tenant_ids: [...model.allowlist_tenant_ids],
    })
  }

  function updateDraft<K extends keyof ModelDraft>(key: K, value: ModelDraft[K]) {
    setDraft((current) => current ? { ...current, [key]: value } : current)
  }

  async function saveModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft) return
    setSaving(true)
    setError('')
    setNotice('')
    const payload = {
      ...draft,
      api_key: draft.api_key.trim() || null,
      context_window: draft.context_window ? Number(draft.context_window) : null,
      max_output_tokens: draft.max_output_tokens ? Number(draft.max_output_tokens) : null,
      default_temperature: draft.default_temperature || null,
      rpm_limit: draft.rpm_limit ? Number(draft.rpm_limit) : null,
      fallback_model_id: draft.fallback_model_id || null,
    }
    try {
      const saved = await apiRequest<PlatformModel>(
        draft.id ? `/api/v1/platform/models/${draft.id}` : '/api/v1/platform/models',
        {
          method: draft.id ? 'PUT' : 'POST',
          body: JSON.stringify(payload),
        },
      )
      await refreshModels()
      setDraft(null)
      setNotice(draft.id ? `已更新模型「${saved.display_name}」。` : `已新增模型「${saved.display_name}」。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存模型失败')
    } finally {
      setSaving(false)
    }
  }

  const visibleModels = models.filter((model) => {
    const textMatches = `${model.display_name} ${model.provider_model_id} ${MODEL_PROVIDERS[model.provider] ?? model.provider}`
      .toLowerCase()
      .includes(search.trim().toLowerCase())
    return textMatches && (!statusFilter || model.status === statusFilter)
  })

  return (
    <section className="platform-models-page">
      <div className="platform-page-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />平台定义 · AI 网关</div>
          <h1>模型目录</h1>
          <p>管理平台可供租户使用的模型、区域、价格和凭证。密钥加密保存，不会再次显示明文。</p>
        </div>
        <button className="primary-button" onClick={() => { setError(''); setDraft(emptyModelDraft()) }} type="button">
          <Plus size={16} />新增模型
        </button>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <div className="model-catalog-toolbar">
        <label className="model-search">搜索模型
          <input onChange={(event) => setSearch(event.target.value)} placeholder="模型名称、供应商或模型 ID" value={search} />
        </label>
        <label>状态
          <select onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}>
            <option value="">全部状态</option>
            <option value="healthy">健康</option>
            <option value="canary">灰度发布</option>
            <option value="disabled">已停用</option>
          </select>
        </label>
        <span className="model-catalog-count">{visibleModels.length} 个模型</span>
      </div>
      <div className="panel model-catalog-panel">
        {loading ? <div className="members-empty">正在读取模型目录…</div> : visibleModels.length === 0 ? (
          <div className="model-catalog-empty">
            <span className="model-empty-mark"><Boxes size={22} /></span>
            <strong>{models.length === 0 ? '模型目录尚未配置' : '没有符合条件的模型'}</strong>
            <p>{models.length === 0 ? '添加一个受支持的模型后，再配置其套餐范围、区域与凭证。' : '调整搜索条件或状态筛选。'}</p>
          </div>
        ) : (
          <div className="members-table-wrap">
            <table className="members-table model-catalog-table">
              <thead><tr><th>模型</th><th>类型与区域</th><th>开放范围</th><th>输入 / 输出价格</th><th>凭证</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{visibleModels.map((model) => (
                <tr key={model.id}>
                  <td><span className="model-name-cell"><strong>{model.display_name}</strong><small>{MODEL_PROVIDERS[model.provider] ?? model.provider} · {model.provider_model_id}</small></span></td>
                  <td>{MODEL_TYPES[model.model_type] ?? model.model_type}<small className="platform-tenant-slug">{MODEL_REGIONS[model.region] ?? model.region}</small></td>
                  <td>{MODEL_AVAILABILITY[model.availability] ?? model.availability}{model.availability === 'allowlist' && <small className="platform-tenant-slug">{model.allowlist_tenant_ids.length} 家企业</small>}</td>
                  <td><span className="model-price-cell">${model.input_price_per_million} / ${model.output_price_per_million}<small>每百万 Token</small></span></td>
                  <td><span className="model-key-mask">{model.credential_configured ? model.api_key_masked : '未配置'}</span></td>
                  <td><span className={`member-status ${model.status === 'healthy' ? 'active' : 'inactive'}`}><i />{model.status === 'healthy' ? '健康' : model.status === 'canary' ? '灰度发布' : '已停用'}</span></td>
                  <td><button className="text-action" onClick={() => editModel(model)} type="button">编辑</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
      {draft && (
        <div className="modal-backdrop model-modal-backdrop" role="presentation" onClick={() => !saving && setDraft(null)}>
          <form className="model-editor" onSubmit={saveModel} role="dialog" aria-modal="true" aria-labelledby="model-editor-title" onClick={(event) => event.stopPropagation()}>
            <header className="model-editor-header">
              <div><div className="section-kicker">AI 网关 · 模型注册</div><h2 id="model-editor-title">{draft.id ? '编辑模型' : '新增模型'}</h2></div>
              <button className="icon-button" aria-label="关闭" disabled={saving} onClick={() => setDraft(null)} type="button"><X size={18} /></button>
            </header>
            <div className="model-editor-scroll">
              <div className="model-editor-section">
                <strong>模型身份</strong>
                <div className="model-editor-grid">
                  <label>显示名称<input maxLength={120} onChange={(event) => updateDraft('display_name', event.target.value)} required value={draft.display_name} /></label>
                  <label>供应商<select onChange={(event) => updateDraft('provider', event.target.value)} value={draft.provider}>{Object.entries(MODEL_PROVIDERS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>模型类型<select onChange={(event) => updateDraft('model_type', event.target.value)} value={draft.model_type}>{Object.entries(MODEL_TYPES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>供应商模型 ID<input maxLength={160} onChange={(event) => updateDraft('provider_model_id', event.target.value)} required value={draft.provider_model_id} /></label>
                  <label className="model-editor-wide">HTTPS 接口地址<input onChange={(event) => updateDraft('endpoint_url', event.target.value)} placeholder="https://api.provider.com/v1" required type="url" value={draft.endpoint_url} /></label>
                  <label>API 版本<input maxLength={80} onChange={(event) => updateDraft('api_version', event.target.value)} placeholder="Azure 等供应商可填写" value={draft.api_version} /></label>
                  <label>部署区域<select onChange={(event) => updateDraft('region', event.target.value)} value={draft.region}>{Object.entries(MODEL_REGIONS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                </div>
              </div>
              <div className="model-editor-section">
                <strong>租户开放与供给</strong>
                <div className="model-editor-grid">
                  <label>开放范围<select onChange={(event) => updateDraft('availability', event.target.value)} value={draft.availability}>{Object.entries(MODEL_AVAILABILITY).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>目录状态<select onChange={(event) => updateDraft('status', event.target.value as PlatformModel['status'])} value={draft.status}><option value="healthy">健康</option><option value="canary">灰度发布</option><option value="disabled">已停用</option></select></label>
                  {draft.availability === 'allowlist' && <fieldset className="model-allowlist"><legend>白名单租户</legend>{tenants.map((tenant) => <label key={tenant.id}><input checked={draft.allowlist_tenant_ids.includes(tenant.id)} onChange={(event) => updateDraft('allowlist_tenant_ids', event.target.checked ? [...draft.allowlist_tenant_ids, tenant.id] : draft.allowlist_tenant_ids.filter((id) => id !== tenant.id))} type="checkbox" />{tenant.name}</label>)}</fieldset>}
                  <label className="model-editor-wide">API Key<input autoComplete="new-password" onChange={(event) => updateDraft('api_key', event.target.value)} placeholder={draft.id ? '留空以保留当前密钥；输入新值则轮换' : '保存后只显示掩码'} required={!draft.id} type="password" value={draft.api_key} /><small>{draft.id ? '明文不会回显；留空不修改现有凭证。' : '凭证会加密保存，API 不会返回明文。'}</small></label>
                </div>
              </div>
              <div className="model-editor-section">
                <strong>价格与运行限制</strong>
                <div className="model-editor-grid">
                  <label>输入成本 / 百万 Token<input min="0" onChange={(event) => updateDraft('input_price_per_million', event.target.value)} required step="0.000001" type="number" value={draft.input_price_per_million} /></label>
                  <label>输出成本 / 百万 Token<input min="0" onChange={(event) => updateDraft('output_price_per_million', event.target.value)} required step="0.000001" type="number" value={draft.output_price_per_million} /></label>
                  <label>输入扣点 / 千 Token<input min="0" onChange={(event) => updateDraft('input_points_per_thousand', event.target.value)} required step="0.000001" type="number" value={draft.input_points_per_thousand} /></label>
                  <label>输出扣点 / 千 Token<input min="0" onChange={(event) => updateDraft('output_points_per_thousand', event.target.value)} required step="0.000001" type="number" value={draft.output_points_per_thousand} /></label>
                  <label>上下文窗口<input min="1" onChange={(event) => updateDraft('context_window', event.target.value)} type="number" value={draft.context_window} /></label>
                  <label>最大输出 Token<input min="1" onChange={(event) => updateDraft('max_output_tokens', event.target.value)} type="number" value={draft.max_output_tokens} /></label>
                  <label>默认温度<input max="2" min="0" onChange={(event) => updateDraft('default_temperature', event.target.value)} step="0.01" type="number" value={draft.default_temperature} /></label>
                  <label>平台 RPM 上限<input min="1" onChange={(event) => updateDraft('rpm_limit', event.target.value)} type="number" value={draft.rpm_limit} /></label>
                  <label className="model-editor-wide">降级模型<select onChange={(event) => updateDraft('fallback_model_id', event.target.value)} value={draft.fallback_model_id}><option value="">不配置降级模型</option>{models.filter((model) => model.id !== draft.id && model.model_type === draft.model_type && model.region === draft.region && model.status !== 'disabled').map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}</select><small>只能选择同类型、同区域模型；服务端会阻止循环引用。</small></label>
                </div>
              </div>
            </div>
            <footer className="model-editor-footer">
              <button className="secondary-button" disabled={saving} onClick={() => setDraft(null)} type="button">取消</button>
              <button className="primary-button" disabled={saving} type="submit">{saving ? '正在保存…' : '保存模型'}</button>
            </footer>
          </form>
        </div>
      )}
    </section>
  )
}

function TenantModelSettingsPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const [environment, setEnvironment] = useState<TenantModelEnvironment['environment']>('production')
  const [config, setConfig] = useState<TenantModelEnvironment | null>(null)
  const [enabledModelIds, setEnabledModelIds] = useState<string[]>([])
  const [defaultModelIds, setDefaultModelIds] = useState<TenantModelEnvironment['default_model_ids']>({
    chat: null,
    embedding: null,
    rerank: null,
  })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<TenantModelEnvironment>(
      `/api/v1/tenant/model-environments/${environment}`,
      {},
      tenantId,
    )
      .then((result) => {
        if (!active) return
        const availableIds = new Set(result.models.map((model) => model.id))
        setConfig(result)
        setEnabledModelIds(result.enabled_model_ids)
        setDefaultModelIds({
          chat: result.default_model_ids.chat && availableIds.has(result.default_model_ids.chat) ? result.default_model_ids.chat : null,
          embedding: result.default_model_ids.embedding && availableIds.has(result.default_model_ids.embedding) ? result.default_model_ids.embedding : null,
          rerank: result.default_model_ids.rerank && availableIds.has(result.default_model_ids.rerank) ? result.default_model_ids.rerank : null,
        })
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [environment, tenantId])

  function selectDefault(type: 'chat' | 'embedding' | 'rerank', modelId: string) {
    setDefaultModelIds((current) => ({ ...current, [type]: modelId || null }))
    if (modelId) setEnabledModelIds((current) => current.includes(modelId) ? current : [...current, modelId])
  }

  async function saveConfiguration() {
    if (!config || !canEdit) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const availableIds = new Set(config.models.map((model) => model.id))
      const enabled = enabledModelIds.filter((id) => availableIds.has(id))
      const saved = await apiRequest<TenantModelEnvironment>(
        `/api/v1/tenant/model-environments/${environment}`,
        {
          method: 'PUT',
          body: JSON.stringify({
            expected_version: config.version,
            enabled_model_ids: enabled,
            default_model_ids: defaultModelIds,
          }),
        },
        tenantId,
      )
      setConfig(saved)
      setEnabledModelIds(saved.enabled_model_ids)
      setNotice('模型环境配置已保存。')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存模型配置失败')
    } finally {
      setSaving(false)
    }
  }

  function changeEnvironment(nextEnvironment: TenantModelEnvironment['environment']) {
    if (nextEnvironment === environment) return
    setLoading(true)
    setConfig(null)
    setEnabledModelIds([])
    setDefaultModelIds({ chat: null, embedding: null, rerank: null })
    setError('')
    setNotice('')
    setEnvironment(nextEnvironment)
  }

  const enabledSet = new Set(enabledModelIds)
  const staleCount = config?.unavailable_enabled_model_ids.length ?? 0
  return (
    <section className="tenant-model-settings">
      <div className="page-heading settings-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />企业设置 · 模型与环境</div>
          <h1>模型与环境</h1>
          <p>按环境选择可用模型，并为对话、向量化和重排序指定默认模型。</p>
        </div>
        <div className="settings-region-stamp">
          <span>{config ? PLAN_LABELS[config.plan] ?? config.plan : '套餐核验中'}</span>
          <strong>{config ? REGION_LABELS[config.region] ?? config.region : '读取区域…'}</strong>
        </div>
      </div>
      <div className="model-environment-tabs" role="tablist" aria-label="选择配置环境">
        {([
          ['development', '开发'],
          ['testing', '测试'],
          ['production', '生产'],
        ] as const).map(([value, label]) => (
          <button
            aria-selected={environment === value}
            className={environment === value ? 'active' : ''}
            key={value}
            onClick={() => changeEnvironment(value)}
            role="tab"
            type="button"
          >
            <i />{label}
          </button>
        ))}
        <span className="model-environment-version">
          {config ? `配置版本 ${config.version || '尚未保存'}` : '环境配置'}
          {config?.updated_at ? ` · ${new Date(config.updated_at).toLocaleDateString('zh-CN')}` : ''}
        </span>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      {staleCount > 0 && (
        <p className="model-environment-warning" role="status">
          有 {staleCount} 个已启用模型已不符合当前套餐、区域或目录状态；保存时会从本环境移除，请重新选择默认模型。
        </p>
      )}
      <div className="panel model-defaults-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">默认路由</div><h2>本环境默认模型</h2></div>
          <span className="model-environment-count">{enabledModelIds.filter((id) => config?.models.some((model) => model.id === id)).length} 个已启用</span>
        </div>
        <div className="model-default-grid">
          {(['chat', 'embedding', 'rerank'] as const).map((type) => {
            const options = config?.models.filter((model) => model.model_type === type && enabledSet.has(model.id)) ?? []
            return (
              <label key={type}>
                <span>{MODEL_TYPE_LABELS[type]}默认模型</span>
                <select
                  disabled={!canEdit || loading || options.length === 0}
                  onChange={(event) => selectDefault(type, event.target.value)}
                  value={defaultModelIds[type] ?? ''}
                >
                  <option value="">{options.length ? '不设置默认模型' : '无可用模型'}</option>
                  {options.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
                </select>
              </label>
            )
          })}
        </div>
        <p className="model-settings-help">默认模型必须同时满足套餐、数据区域和模型类型要求，并且已在当前环境启用。</p>
      </div>
      <div className="panel tenant-model-list-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">平台模型目录</div><h2>本环境可用模型</h2></div>
          <span className="model-environment-count">{config?.models.length ?? 0} 个符合条件</span>
        </div>
        {loading ? <div className="members-empty">正在读取当前环境可用模型…</div> : !config?.models.length ? (
          <div className="model-catalog-empty">
            <strong>当前没有可用模型</strong>
            <p>请联系平台管理员检查模型目录、套餐开放范围和数据区域。</p>
          </div>
        ) : (
          <div className="tenant-model-list">
            {config.models.map((model) => {
              const enabled = enabledSet.has(model.id)
              const isDefault = Object.values(defaultModelIds).includes(model.id)
              return (
                <label className={`tenant-model-row${enabled ? ' enabled' : ''}`} key={model.id}>
                  <input
                    checked={enabled}
                    disabled={!canEdit}
                    onChange={(event) => {
                      setEnabledModelIds((current) => event.target.checked ? [...current, model.id] : current.filter((id) => id !== model.id))
                      if (!event.target.checked) {
                        setDefaultModelIds((current) => ({
                          chat: current.chat === model.id ? null : current.chat,
                          embedding: current.embedding === model.id ? null : current.embedding,
                          rerank: current.rerank === model.id ? null : current.rerank,
                        }))
                      }
                    }}
                    type="checkbox"
                  />
                  <span className="tenant-model-mark"><Bot size={17} /></span>
                  <span className="tenant-model-copy">
                    <strong>{model.display_name}{isDefault && <em>默认</em>}</strong>
                    <small>{model.provider} · {MODEL_TYPE_LABELS[model.model_type] ?? model.model_type} · {REGION_LABELS[model.region] ?? model.region}</small>
                  </span>
                  <span className="tenant-model-price">输入 ¥{model.input_price_per_million} / 1M tokens</span>
                  <span className={`tenant-model-state${enabled ? ' on' : ''}`}>{enabled ? '已启用' : '未启用'}</span>
                </label>
              )
            })}
          </div>
        )}
        <div className="model-settings-footer">
          <span>{canEdit ? '修改后保存仅影响当前环境。' : '你有权查看配置，但没有修改权限。'}</span>
          {canEdit && <button className="primary-button" disabled={loading || saving || !config} onClick={() => void saveConfiguration()} type="button">{saving ? '正在保存…' : '保存当前环境'}</button>}
        </div>
      </div>
      <p className="model-settings-security">上游密钥仅由平台服务端管理，不会通过租户设置接口返回。BYOK 和跨环境发布将在密钥管理及审批能力完成后接入。</p>
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

type AuditRecord = {
  id: string
  category: string
  result: 'success' | 'warning' | 'failure'
  created_at: string
  actor_name: string
  actor_email: string | null
  description: string
  source_ip: string | null
  details: Record<string, string>
}

type AuditRecordsResponse = {
  events: AuditRecord[]
  total: number
  limit: number
  offset: number
}

type AuditIntegrityResponse = {
  chain_scope: string
  is_valid: boolean
  checked_events: number
  unchained_legacy_events: number
  through_sequence: number
}

type AuditFilters = {
  category: string
  search: string
  result: string
  start: string
  end: string
}

const emptyAuditFilters: AuditFilters = {
  category: '',
  search: '',
  result: '',
  start: '',
  end: '',
}

function auditQuery(filters: AuditFilters) {
  const params = new URLSearchParams()
  if (filters.category) params.set('category', filters.category)
  if (filters.search.trim()) params.set('search', filters.search.trim())
  if (filters.result) params.set('result', filters.result)
  if (filters.start) params.set('start', `${filters.start}T00:00:00Z`)
  if (filters.end) params.set('end', `${filters.end}T23:59:59Z`)
  return params
}

function AuditLogPage({
  tenantId,
  canExport,
  platformMode = false,
}: {
  tenantId?: string
  canExport: boolean
  platformMode?: boolean
}) {
  const [filters, setFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [appliedFilters, setAppliedFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [offset, setOffset] = useState(0)
  const [requestVersion, setRequestVersion] = useState(0)
  const [data, setData] = useState<AuditRecordsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [integrity, setIntegrity] = useState<AuditIntegrityResponse | null>(null)
  const [integrityLoading, setIntegrityLoading] = useState(true)
  const [integrityError, setIntegrityError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const params = auditQuery(appliedFilters)
    params.set('limit', '50')
    params.set('offset', String(offset))
    const endpoint = platformMode ? '/api/v1/platform/audit/events' : '/api/v1/audit/events'
    apiRequest<AuditRecordsResponse>(`${endpoint}?${params}`, {}, tenantId)
      .then((response) => active && setData(response))
      .catch((reason: Error) => {
        if (!active) return
        setData(null)
        setError(reason.message)
      })
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [appliedFilters, offset, platformMode, requestVersion, tenantId])

  useEffect(() => {
    let active = true
    const endpoint = platformMode
      ? '/api/v1/platform/audit/integrity'
      : '/api/v1/audit/integrity'
    apiRequest<AuditIntegrityResponse>(endpoint, {}, tenantId)
      .then((response) => active && setIntegrity(response))
      .catch((reason: Error) => {
        if (!active) return
        setIntegrity(null)
        setIntegrityError(reason.message)
      })
      .finally(() => active && setIntegrityLoading(false))
    return () => {
      active = false
    }
  }, [platformMode, tenantId])

  function updateFilter(key: keyof AuditFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }))
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setLoading(true)
    setOffset(0)
    setAppliedFilters({ ...filters })
    setRequestVersion((version) => version + 1)
  }

  function goToOffset(nextOffset: number) {
    setError('')
    setLoading(true)
    setOffset(nextOffset)
  }

  async function exportCsv() {
    setExporting(true)
    setError('')
    try {
      const params = auditQuery(appliedFilters)
      const endpoint = platformMode
        ? '/api/v1/platform/audit/events/export'
        : '/api/v1/audit/events/export'
      const response = await fetch(`${endpoint}?${params}`, {
        credentials: 'same-origin',
        headers: tenantId ? { 'X-Tenant-ID': tenantId } : undefined,
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '导出审计日志失败')
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = platformMode ? 'beework-platform-audit.csv' : 'beework-audit.csv'
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导出审计日志失败')
    } finally {
      setExporting(false)
    }
  }

  const page = data ? Math.floor(offset / data.limit) + 1 : 1
  const pageCount = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1
  const pageSize = data?.limit ?? 50

  return (
    <section className="audit-page">
      <div className="welcome-row audit-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />{platformMode ? '平台治理 · 操作记录' : '安全与治理 · 操作记录'}</div>
          <h1>审计日志</h1>
          <p>{platformMode ? '平台级操作与企业业务审计分开存储和查询。' : '配置变更与安全事件按时间倒序记录。'}历史数据无法提供的来源 IP 会显示为「—」。</p>
        </div>
        {canExport && <button className="primary-button" type="button" disabled={exporting} onClick={exportCsv}><Download size={16} />{exporting ? '正在导出' : '导出 CSV'}</button>}
      </div>

      <div className={`audit-integrity${integrityError || (integrity && !integrity.is_valid) ? ' invalid' : ''}`} aria-live="polite">
        <ShieldCheck size={17} />
        <strong>哈希链完整性</strong>
        <span>
          {integrityLoading
            ? '正在校验…'
            : integrityError
              ? `校验失败：${integrityError}`
              : integrity?.is_valid
                ? `链上 ${integrity.checked_events} 条记录通过校验`
                : '检测到哈希链异常，请联系平台管理员'}
        </span>
        {integrity && integrity.unchained_legacy_events > 0 && (
          <small>{integrity.unchained_legacy_events} 条历史记录未纳入哈希链</small>
        )}
      </div>

      <form className="audit-filters" onSubmit={applyFilters}>
        <label>分类
          <select value={filters.category} onChange={(event) => updateFilter('category', event.target.value)}>
            <option value="">全部分类</option>
            <option value="assistants">助手</option>
            <option value="security">安全</option>
            <option value="members_permissions">成员与权限</option>
            <option value="billing">计费</option>
            <option value="developer">开发者</option>
          </select>
        </label>
        <label className="audit-search">操作人或操作内容
          <input value={filters.search} onChange={(event) => updateFilter('search', event.target.value)} placeholder="搜索操作人或操作内容…" />
        </label>
        <label>结果
          <select value={filters.result} onChange={(event) => updateFilter('result', event.target.value)}>
            <option value="">全部结果</option>
            <option value="success">成功</option>
            <option value="warning">告警</option>
            <option value="failure">失败或驳回</option>
          </select>
        </label>
        <label>开始日期<input type="date" value={filters.start} onChange={(event) => updateFilter('start', event.target.value)} /></label>
        <label>结束日期<input type="date" value={filters.end} onChange={(event) => updateFilter('end', event.target.value)} /></label>
        <button className="secondary-button" type="submit">筛选</button>
      </form>

      {error && <div className="members-error" role="alert">{error}</div>}
      <div className="audit-table-card">
        <div className="audit-table-meta"><strong>操作记录</strong><span>{data?.total ?? 0} 条</span></div>
        {loading ? <div className="audit-empty">正在加载审计记录…</div> : data?.events.length ? (
          <div className="audit-table-scroll">
            <table className="audit-table">
              <thead><tr><th>结果</th><th>时间</th><th>分类</th><th>操作人 / 操作描述</th><th>来源 IP</th><th>变更</th></tr></thead>
              <tbody>{data.events.map((entry) => (
                <tr key={entry.id}>
                  <td><span className={`audit-result ${entry.result}`}><i />{entry.result === 'success' ? '成功' : entry.result === 'warning' ? '告警' : '失败'}</span></td>
                  <td><time dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleString('zh-CN', { hour12: false })}</time></td>
                  <td><span className="audit-category">{({ assistants: '助手', security: '安全', members_permissions: '成员与权限', billing: '计费', developer: '开发者' } as Record<string, string>)[entry.category] ?? entry.category}</span></td>
                  <td><span className="audit-actor"><strong>{entry.actor_name}</strong><small>{entry.actor_email ?? '系统操作'} · {entry.description}</small></span></td>
                  <td>{entry.source_ip ?? '—'}</td>
                  <td><details className="audit-details"><summary>查看</summary><pre>{JSON.stringify(entry.details, null, 2)}</pre></details></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <div className="audit-empty">{error ? '审计记录加载失败' : '没有匹配的记录'}</div>}
        <div className="audit-pagination">
          <span>第 {page} / {pageCount} 页</span>
          <div>
            <button className="secondary-button" type="button" disabled={!data || offset === 0 || loading} onClick={() => goToOffset(Math.max(0, offset - pageSize))}>上一页</button>
            <button className="secondary-button" type="button" disabled={!data || offset + pageSize >= data.total || loading} onClick={() => goToOffset(offset + pageSize)}>下一页</button>
          </div>
        </div>
      </div>
    </section>
  )
}

function SectionPlaceholder({ title, icon }: { title: string; icon: React.ReactNode }) {
  const [modalOpen, setModalOpen] = useState(false)
  const isAccessDenied = title === '无权访问'
  return (
    <section className="placeholder-page">
      <div className="placeholder-icon">{icon}</div>
      <div className="section-kicker">工作空间</div>
      <h1>{title}</h1>
      <p>{isAccessDenied ? '当前账号没有访问此功能的权限，请联系对应的企业管理员或平台管理员。' : '前端框架已就绪，这个模块会在后续开发中接入真实 API。'}</p>
      {!isAccessDenied && (
        <button className="primary-button" type="button" onClick={() => setModalOpen(true)}><Plus size={17} />创建{title === '助理' ? '助理' : title === '知识库' ? '知识库' : '新项目'}</button>
      )}
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
