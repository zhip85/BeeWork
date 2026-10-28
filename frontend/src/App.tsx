import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  ArrowRight,
  Bell,
  BookOpen,
  Bot,
  Boxes,
  ChevronDown,
  CircleHelp,
  Command,
  FileText,
  Hexagon,
  LayoutDashboard,
  LogOut,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Workflow,
} from 'lucide-react'
import { Link, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { apiRequest } from './api'
import { applyLocale, currentLocale, enumLabel, isLocale } from './i18n'
import { LanguageSwitch } from './i18n/LanguageSwitch'
import { SectionPlaceholder } from './components/SectionPlaceholder'
import { AuditLogPage } from './pages/audit/AuditLogPage'
import { InviteAcceptancePage } from './pages/auth/InviteAcceptancePage'
import { LoginPage } from './pages/auth/LoginPage'
import { NoTenantAccessPage } from './pages/auth/NoTenantAccessPage'
import { Dashboard } from './pages/dashboard/Dashboard'
import { MembersPage } from './pages/members/MembersPage'
import { PlatformConsole } from './pages/platform/PlatformConsole'
import { TenantModelSettingsPage } from './pages/settings/TenantModelSettingsPage'
import { roleLabel } from './roles'
import type { CurrentUser } from './types'
import './App.css'

const navigation = [
  { labelKey: 'dashboard', to: '/', icon: LayoutDashboard, end: true, permission: 'dashboard:view' },
  { labelKey: 'assistants', to: '/assistants', icon: Bot, permission: 'assistants:view' },
  { labelKey: 'workflows', to: '/workflows', icon: Workflow, permission: 'orchestration:view' },
  { labelKey: 'knowledge', to: '/knowledge', icon: BookOpen, permission: 'knowledge:view' },
  { labelKey: 'capabilities', to: '/capabilities', icon: Boxes, permission: 'capabilities:view' },
]

function App() {
  const { t } = useTranslation(['nav', 'common'])
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
      : location.pathname === '/settings'
      ? t('nav:modelSettings')
      : t(`nav:${navigation.find((item) =>
          item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
        )?.labelKey ?? 'dashboard'}`)

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
            <small>{activeTenant ? roleLabel(activeTenant.role, activeTenant.custom_role_name) : t('nav:contactPlatformAdmin')}</small>
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
          {navigation.filter((item) => hasPermission(item.permission)).map(({ labelKey, to, icon: Icon, end }) => (
            <NavLink
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              end={end}
              key={to}
              to={to}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{t(`nav:${labelKey}`)}</span>
              {labelKey === 'assistants' && <span className="nav-count">12</span>}
            </NavLink>
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
            <LanguageSwitch persist />
            <button className="search-button" type="button" aria-label={t('common:search')}>
              <Search size={16} /><span>{t('common:searchAnything')}</span><kbd><Command size={11} /> K</kbd>
            </button>
            <span className={`service-status ${apiStatus}`} title={`API ${apiStatus}`}>
              <i />{apiStatus === 'online' ? t('common:apiOnline') : apiStatus === 'offline' ? t('common:apiOffline') : t('common:apiChecking')}
            </span>
            <button className="icon-button notification-button" type="button" aria-label={t('common:notifications')}>
              <Bell size={18} /><i />
            </button>
            <button className="help-button" type="button"><CircleHelp size={17} />{t('common:help')}</button>
          </div>
        </header>
        <div className="page-content">
          {activeTenant?.access_mode === 'read_only' && (
            <p className="tenant-readonly-banner" role="status">
              {t('nav:readonlyBanner', { status: enumLabel('tenantStatus', activeTenant.tenant_status) })}
            </p>
          )}
          <Routes>
            <Route path="/" element={hasPermission('dashboard:view') ? <Dashboard user={user} tenant={activeTenant} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            {navigation.slice(1).map(({ labelKey, to, icon: Icon, permission }) => (
              <Route key={to} path={to} element={hasPermission(permission) ? <SectionPlaceholder title={t(`nav:${labelKey}`)} icon={<Icon size={22} />} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            ))}
            <Route path="/operations" element={canViewOperations ? <SectionPlaceholder title={t('nav:operations')} icon={<Activity size={22} />} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/audit" element={hasPermission('audit:view') && activeTenant ? <AuditLogPage key={activeTenant.id} tenantId={activeTenant.id} canExport={hasPermission('audit:export')} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
            <Route path="/settings" element={canViewSettings && activeTenant ? <TenantModelSettingsPage key={activeTenant.id} tenantId={activeTenant.id} canEdit={canEditSettings} /> : <SectionPlaceholder denied icon={<ShieldCheck size={22} />} />} />
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
