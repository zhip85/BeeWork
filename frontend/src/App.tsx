import { Fragment, Suspense, lazy, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  ArrowRight,
  BookOpen,
  Bot,
  Boxes,
  ChevronDown,
  CircleHelp,
  ClipboardCheck,
  Command,
  FileText,
  Globe2,
  Hexagon,
  Radio,
  LayoutDashboard,
  LogOut,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Workflow,
} from 'lucide-react'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { apiRequest, setCurrentEnvironment } from './api'
import { applyLocale, currentLocale, enumLabel, isLocale } from './i18n'
import { LanguageSwitch } from './i18n/LanguageSwitch'
import { SectionPlaceholder } from './components/SectionPlaceholder'
import { AssistantsPage } from './pages/assistants/AssistantsPage'
import { ChannelsPage } from './pages/channels/ChannelsPage'
import { ApprovalsPage } from './pages/approvals/ApprovalsPage'
import { EnvironmentSwitcher } from './components/EnvironmentSwitcher'
import { NotificationBell } from './components/NotificationBell'
import { NotificationsPage } from './pages/notifications/NotificationsPage'
import { AssistantDetailPage } from './pages/assistants/AssistantDetailPage'
import { VersionsPage } from './pages/assistants/VersionsPage'
import { OrchestrationPage } from './pages/orchestration/OrchestrationPage'
import { ConversationsPage } from './pages/assistants/ConversationsPage'
import { AuditLogPage } from './pages/audit/AuditLogPage'
import { InviteAcceptancePage } from './pages/auth/InviteAcceptancePage'
import { LoginPage } from './pages/auth/LoginPage'
import { NoTenantAccessPage } from './pages/auth/NoTenantAccessPage'
import { Dashboard } from './pages/dashboard/Dashboard'
import { MembersPage } from './pages/members/MembersPage'
import { PlatformConsole } from './pages/platform/PlatformConsole'
import { CapabilitiesPage } from './pages/capabilities/CapabilitiesPage'
import { FlowConnectionFormPage } from './pages/capabilities/FlowConnectionFormPage'
import { McpFormPage } from './pages/capabilities/McpFormPage'
import { SkillFormPage } from './pages/capabilities/SkillFormPage'
import { PortalAdminPage } from './pages/portal-admin/PortalAdminPage'
import { FlowCreatePage } from './pages/flow-studio/FlowCreatePage'
import { FlowEditorPage } from './pages/flow-studio/FlowEditorPage'
import { FlowsPage } from './pages/flow-studio/FlowsPage'
import { KnowledgeCreatePage } from './pages/knowledge/KnowledgeCreatePage'
import { KnowledgeDetailPage } from './pages/knowledge/KnowledgeDetailPage'
import { KnowledgeListPage } from './pages/knowledge/KnowledgeListPage'
import { ApiKeysPage } from './pages/settings/ApiKeysPage'
import { ApprovalPoliciesPage } from './pages/settings/ApprovalPoliciesPage'
import { DataPrivacyPage } from './pages/settings/DataPrivacyPage'
import { TenantGlossaryPage } from './pages/settings/TenantGlossaryPage'
import { TenantPiiRulesPage } from './pages/settings/TenantPiiRulesPage'
import { TenantModelSettingsPage } from './pages/settings/TenantModelSettingsPage'
import { UsagePage } from './pages/settings/UsagePage'
import { roleLabel } from './roles'
import type { CurrentUser, TenantEnvironmentName } from './types'
import './App.css'
import './design-system.css'

// The employee portal is its own bundle: employees never download the console, and the console never loads portal styles.
const PortalApp = lazy(() => import('./portal/PortalApp'))
const PortalLogin = lazy(() => import('./portal/PortalLogin'))

const navigation = [
  { labelKey: 'dashboard', to: '/', icon: LayoutDashboard, end: true, permission: 'dashboard:view', group: '' },
  { labelKey: 'assistants', to: '/assistants', icon: Bot, permission: 'assistants:view', group: 'build' },
  { labelKey: 'knowledge', to: '/knowledge', icon: BookOpen, permission: 'knowledge:view', group: 'build' },
  { labelKey: 'flowStudio', to: '/flow-studio', icon: Workflow, permission: 'agents:view', group: 'build' },
  { labelKey: 'capabilities', to: '/capabilities', icon: Boxes, permission: 'capabilities:view', group: 'build' },
  { labelKey: 'aiPortal', to: '/ai-portal', icon: Globe2, permission: 'portal:view', group: 'run' },
  { labelKey: 'channels', to: '/channels', icon: Radio, permission: 'channels:view', group: 'run' },
  { labelKey: 'approvals', to: '/approvals', icon: ClipboardCheck, permission: 'approvals:view', group: 'run' },
]

// Navigation entries with their own page; every other entry still shows the placeholder (listing them here is what
// keeps a placeholder route from shadowing the real one at the same path).
const REAL_PAGES = new Set(['/assistants', '/capabilities', '/ai-portal', '/flow-studio', '/approvals', '/channels', '/knowledge'])

function App() {
  const { t } = useTranslation(['nav', 'common', 'settings', 'notifications', 'channels'])
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
  const navigate = useNavigate()
  const [environmentChoice, setEnvironmentChoice] = useState<Record<string, TenantEnvironmentName>>(() => {
    try { return JSON.parse(localStorage.getItem('beework.environments') ?? '{}') } catch { return {} }
  })
  // Only an environment the plan offers is ever used; anything stale falls back to production.
  const environment: TenantEnvironmentName =
    activeTenant && environmentChoice[activeTenant.id] && activeTenant.environments.includes(environmentChoice[activeTenant.id])
      ? environmentChoice[activeTenant.id]
      : 'production'
  // Set during render (not in an effect) so child pages' first requests already carry the right header.
  setCurrentEnvironment(activeTenant ? environment : null)
  function handleEnvironmentChange(next: TenantEnvironmentName) {
    if (!activeTenant) return
    const updated = { ...environmentChoice, [activeTenant.id]: next }
    setEnvironmentChoice(updated)
    try { localStorage.setItem('beework.environments', JSON.stringify(updated)) } catch { /* private mode */ }
  }
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [approvalsInvolved, setApprovalsInvolved] = useState(false)
  const [approvalsTick, setApprovalsTick] = useState(0)
  const hasPermission = (permission: string) => Boolean(activeTenant?.permissions.includes(permission))
  const canViewSettings = hasPermission('models:view')
  const canEditSettings = hasPermission('models:edit') && activeTenant?.access_mode === 'full'
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
      ? t('nav:members')
      : location.pathname === '/audit'
      ? t('nav:audit')
      : location.pathname === '/platform/tenants'
      ? t('nav:platformTenants')
      : location.pathname === '/notifications'
      ? t('notifications:title')
      : location.pathname === '/settings/api'
      ? t('channels:keys.title')
      : location.pathname === '/settings/usage'
      ? t('settings:tabs.usage')
      : location.pathname === '/settings/approvals'
      ? t('settings:tabs.approvals')
      : location.pathname === '/settings/privacy'
      ? t('settings:tabs.privacy')
      : location.pathname === '/settings/rules'
      ? t('settings:tabs.rules')
      : location.pathname === '/settings/glossary'
      ? t('settings:tabs.glossary')
      : location.pathname === '/settings'
      ? t('nav:modelSettings')
      : t(`nav:${navigation.find((item) =>
          item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
        )?.labelKey ?? 'dashboard'}`)

  const canSeeApprovals = Boolean(activeTenant?.permissions.includes('approvals:view')) || approvalsInvolved
  const activeTenantKey = activeTenant?.id
  useEffect(() => {
    if (!user || !activeTenantKey) return
    let active = true
    // Approvers are defined by the approval policy, not module permissions (PER-001), so this is not permission-gated.
    const load = () => apiRequest<{ pending_mine: number; visible_count: number }>('/api/v1/approvals/summary', {}, activeTenantKey, true)
      .then((result) => {
        if (!active) return
        setPendingApprovals(result.pending_mine)
        setApprovalsInvolved(result.visible_count > 0)
      })
      .catch(() => active && setPendingApprovals(0))
    void load()
    const timer = window.setInterval(() => void load(), 60000)
    return () => { active = false; window.clearInterval(timer) }
  }, [user, activeTenantKey, approvalsTick])

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
        if (isLocale(currentUser.locale) && currentUser.locale !== currentLocale()) {
          void applyLocale(currentUser.locale)
        }
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

  useEffect(() => {
    const onUnauthorized = () => {
      setUser((current) => {
        if (current) setSessionError(t('common:sessionEnded'))
        return null
      })
    }
    window.addEventListener('beework:unauthorized', onUnauthorized)
    return () => window.removeEventListener('beework:unauthorized', onUnauthorized)
  }, [t])

  async function handleLogin(email: string, password: string) {
    const currentUser = await apiRequest<CurrentUser>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
    setUser(currentUser)
    setSessionError('')
    if (isLocale(currentUser.locale) && currentUser.locale !== currentLocale()) {
      void applyLocale(currentUser.locale)
    }
    const firstTenant = currentUser.memberships[0]?.id ?? ''
    setActiveTenantId(firstTenant)
    if (firstTenant) localStorage.setItem('beework.activeTenant', firstTenant)
  }

  async function handleLogout() {
    try {
      await apiRequest<void>('/api/v1/auth/logout', { method: 'POST' })
      setUser(null)
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : t('common:logoutFailed'))
    }
  }

  function handleTenantChange(tenantId: string) {
    setActiveTenantId(tenantId)
    localStorage.setItem('beework.activeTenant', tenantId)
    navigate('/')
  }

  if (sessionLoading) {
    return <div className="auth-loading"><span className="brand-mark"><Hexagon size={22} /><span /></span>{t('common:connectingWorkspace')}</div>
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
    if (location.pathname.startsWith('/portal')) {
      return (
        <Suspense fallback={<div className="auth-loading">{t('common:connectingWorkspace')}</div>}>
          <PortalLogin
            error={sessionError}
            onLogin={async (email, password) => {
              await handleLogin(email, password)
              if (location.pathname === '/portal/login') navigate('/portal', { replace: true })
            }}
          />
        </Suspense>
      )
    }
    return <LoginPage onLogin={handleLogin} error={sessionError} />
  }

  if (!user.is_platform_admin && !user.memberships.length) {
    return <NoTenantAccessPage user={user} onLogout={handleLogout} />
  }

  const inPortal = location.pathname === '/portal' || location.pathname.startsWith('/portal/')
  if (inPortal && activeTenant) {
    return (
      <Suspense fallback={<div className="auth-loading">{t('common:connectingWorkspace')}</div>}>
        <PortalApp key={activeTenant.id} onLogout={handleLogout} tenant={activeTenant} user={user} />
      </Suspense>
    )
  }
  // A portal-only employee has no console: send them to the portal wherever they land.
  if (activeTenant?.role === 'employee' && !user.is_platform_admin) {
    return <Navigate to="/portal" replace />
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
        <Link className="brand" to="/" aria-label={t('nav:brandHome')}>
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
            <strong>{activeTenant?.name ?? t('nav:noTenant')}</strong>
            <small>{activeTenant ? enumLabel('plan', activeTenant.plan) : t('nav:contactPlatformAdmin')}</small>
          </span>
          {user.memberships.length > 1 ? (
            <select
              aria-label={t('nav:switchWorkspace')}
              value={activeTenant?.id ?? ''}
              onChange={(event) => handleTenantChange(event.target.value)}
            >
              {user.memberships.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}
            </select>
          ) : <ChevronDown size={15} />}
        </label>

        <div className="nav-caption">{t('nav:workspace')}</div>
        <nav className="primary-nav" aria-label={t('nav:mainNav')}>
          {navigation.filter((item) => hasPermission(item.permission) || (item.to === '/approvals' && approvalsInvolved)).map(({ labelKey, to, icon: Icon, end, group }, index, visible) => (
            <Fragment key={to}>
            {group && visible[index - 1]?.group !== group && <div className="nav-group-caption">{t(`nav:group.${group}`)}</div>}
            <NavLink
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              end={end}
              to={to}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{t(`nav:${labelKey}`)}</span>
              {to === '/approvals' && pendingApprovals > 0 && <b className="nav-badge" aria-label={t('nav:pendingApprovals', { count: pendingApprovals })}>{pendingApprovals}</b>}
            </NavLink>
            </Fragment>
          ))}
        </nav>

        <div className="nav-caption nav-caption-lower">{t('nav:manage')}</div>
        {canViewOperations && (
          <NavLink className="nav-link" to="/operations">
            <Activity size={18} strokeWidth={1.8} />
            <span>{t('nav:operations')}</span>
          </NavLink>
        )}
        {canViewSettings && (
          <NavLink className="nav-link" to="/settings">
            <Settings2 size={18} strokeWidth={1.8} />
            <span>{t('nav:settings')}</span>
          </NavLink>
        )}
        {hasPermission('audit:view') && (
          <NavLink className="nav-link" to="/audit">
            <FileText size={18} strokeWidth={1.8} />
            <span>{t('nav:audit')}</span>
          </NavLink>
        )}
        {isTenantAdmin && (
          <NavLink className="nav-link" to="/members">
            <ShieldCheck size={18} strokeWidth={1.8} />
            <span>{t('nav:members')}</span>
          </NavLink>
        )}
        {user.is_platform_admin && (
          <NavLink className="nav-link" to="/platform">
            <Boxes size={18} strokeWidth={1.8} />
            <span>{t('nav:platform')}</span>
          </NavLink>
        )}

        <div className="sidebar-spacer" />
        <div className="plan-card">
          <div className="plan-icon"><Sparkles size={16} /></div>
          <strong>{t('nav:planTitle')}</strong>
          <span>{t('nav:planBody')}</span>
          <Link className="plan-link" to="/capabilities">
            {t('nav:viewCapabilities')} <ArrowRight size={14} />
          </Link>
        </div>
        <button className="profile-button" type="button" onClick={handleLogout} title={t('common:logout')}>
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
            {activeTenant && <EnvironmentSwitcher onChange={handleEnvironmentChange} tenantId={activeTenant.id} value={environment} />}
            <LanguageSwitch persist />
            <button className="search-button" type="button" aria-label={t('common:search')}>
              <Search size={16} /><span>{t('common:searchAnything')}</span><kbd><Command size={11} /> K</kbd>
            </button>
            <span className={`service-status ${apiStatus}`} title={`API ${apiStatus}`}>
              <i />{apiStatus === 'online' ? t('common:apiOnline') : apiStatus === 'offline' ? t('common:apiOffline') : t('common:apiChecking')}
            </span>
            {activeTenant && <NotificationBell key={activeTenant.id} tenantId={activeTenant.id} />}
            <button className="help-button" type="button"><CircleHelp size={17} />{t('common:help')}</button>
          </div>
        </header>
        <div className="page-content">
          {activeTenant?.access_mode === 'read_only' && (
            <p className="tenant-readonly-banner" role="status">
              {t('nav:readonlyBanner', { status: enumLabel('tenantStatus', activeTenant.tenant_status) })}
            </p>
          )}
          <Routes key={`${activeTenant?.id ?? ''}:${environment}`}>
            <Route path="/" element={hasPermission('dashboard:view') ? <Dashboard user={user} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants" element={hasPermission('assistants:view') && activeTenant ? <AssistantsPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            {navigation.slice(1).filter((item) => !REAL_PAGES.has(item.to)).map(({ labelKey, to, icon: Icon, permission }) => (
              <Route key={to} path={to} element={hasPermission(permission) ? <SectionPlaceholder title={t(`nav:${labelKey}`)} icon={<Icon size={22} />} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            ))}
            <Route path="/operations" element={canViewOperations ? <SectionPlaceholder title={t('nav:operations')} icon={<Activity size={22} />} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities" element={hasPermission('capabilities:view') && activeTenant ? <CapabilitiesPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/skill/new" element={hasPermission('capabilities:edit') && activeTenant && activeTenant.access_mode === 'full' ? <SkillFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/skill/:capabilityId" element={hasPermission('capabilities:view') && activeTenant ? <SkillFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/mcp/new" element={hasPermission('capabilities:edit') && activeTenant && activeTenant.access_mode === 'full' ? <McpFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/mcp/:capabilityId" element={hasPermission('capabilities:view') && activeTenant ? <McpFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/chatflow/new" element={hasPermission('capabilities:edit') && activeTenant && activeTenant.access_mode === 'full' ? <FlowConnectionFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/chatflow/:connectionId" element={hasPermission('capabilities:view') && activeTenant ? <FlowConnectionFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/workflow/new" element={hasPermission('capabilities:edit') && activeTenant && activeTenant.access_mode === 'full' ? <FlowConnectionFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/capabilities/workflow/:connectionId" element={hasPermission('capabilities:view') && activeTenant ? <FlowConnectionFormPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/ai-portal" element={hasPermission('portal:view') && activeTenant ? <PortalAdminPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/flow-studio" element={hasPermission('agents:view') && activeTenant ? <FlowsPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/flow-studio/new" element={hasPermission('agents:edit') && activeTenant && activeTenant.access_mode === 'full' ? <FlowCreatePage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/flow-studio/:flowId" element={hasPermission('agents:view') && activeTenant ? <FlowEditorPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/knowledge" element={hasPermission('knowledge:view') && activeTenant ? <KnowledgeListPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/knowledge/new" element={hasPermission('knowledge:edit') && activeTenant && activeTenant.access_mode === 'full' ? <KnowledgeCreatePage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/knowledge/:kbId" element={hasPermission('knowledge:view') && activeTenant ? <KnowledgeDetailPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/audit" element={hasPermission('audit:view') && activeTenant ? <AuditLogPage key={activeTenant.id} tenantId={activeTenant.id} canExport={hasPermission('audit:export')} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/notifications" element={activeTenant ? <NotificationsPage key={activeTenant.id} tenantId={activeTenant.id} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/channels" element={hasPermission('channels:view') && activeTenant ? <ChannelsPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/api" element={hasPermission('settings:view') && activeTenant ? <ApiKeysPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={hasPermission('settings:edit') && activeTenant.access_mode === 'full'} environments={activeTenant.environments} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/approvals" element={canSeeApprovals && activeTenant ? <ApprovalsPage key={activeTenant.id} onChanged={() => setApprovalsTick((value) => value + 1)} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/usage" element={hasPermission('settings:view') && activeTenant ? <UsagePage key={activeTenant.id} tenantId={activeTenant.id} canEdit={hasPermission('settings:edit') && activeTenant.access_mode === 'full'} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/approvals" element={hasPermission('settings:view') && activeTenant ? <ApprovalPoliciesPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={isTenantAdmin && activeTenant.access_mode === 'full'} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/glossary" element={hasPermission('settings:view') && activeTenant ? <TenantGlossaryPage key={activeTenant.id} tenantId={activeTenant.id} canDelete={hasPermission('settings:delete') && activeTenant.access_mode === 'full'} canEdit={hasPermission('settings:edit') && activeTenant.access_mode === 'full'} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/rules" element={hasPermission('settings:view') && activeTenant ? <TenantPiiRulesPage key={activeTenant.id} tenantId={activeTenant.id} canDelete={hasPermission('settings:delete') && activeTenant.access_mode === 'full'} canEdit={hasPermission('settings:edit') && activeTenant.access_mode === 'full'} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings/privacy" element={hasPermission('settings:view') && activeTenant ? <DataPrivacyPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={hasPermission('settings:edit') && activeTenant.access_mode === 'full'} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants/:assistantId" element={hasPermission('assistants:view') && activeTenant ? <AssistantDetailPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants/:assistantId/oc" element={hasPermission('orchestration:view') && activeTenant ? <OrchestrationPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants/:assistantId/oc/:stage" element={hasPermission('orchestration:view') && activeTenant ? <OrchestrationPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants/:assistantId/versions" element={hasPermission('assistants:view') && activeTenant ? <VersionsPage key={activeTenant.id} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/assistants/:assistantId/conversations" element={hasPermission('conversations:view') && activeTenant ? <ConversationsPage key={activeTenant.id} tenantId={activeTenant.id} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings" element={canViewSettings && activeTenant ? <TenantModelSettingsPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={canEditSettings} defaultEnvironment={environment} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/members" element={isTenantAdmin && activeTenant ? <MembersPage key={activeTenant.id} tenant={activeTenant} currentUserId={user.id} onMembershipChanged={async () => {
              const refreshed = await apiRequest<CurrentUser>('/api/v1/auth/me')
              setUser(refreshed)
            }} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/platform/*" element={<SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="*" element={<SectionPlaceholder title={t('nav:dashboard')} icon={<LayoutDashboard size={22} />} />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}

export default App
