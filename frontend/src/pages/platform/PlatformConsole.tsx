import {
  Building2,
  Boxes,
  FileText,
  Hexagon,
  LayoutDashboard,
  LogOut,
  ListRestart,
  ShieldCheck,
} from 'lucide-react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../../i18n/LanguageSwitch'
import { AuditLogPage } from '../audit/AuditLogPage'
import { PlatformModelsPage } from './PlatformModelsPage'
import { PlatformOverviewPage } from './PlatformOverviewPage'
import { PlatformTenantsPage } from './PlatformTenantsPage'
import { PlatformJobsPage } from './PlatformJobsPage'
import type { CurrentUser } from '../../types'

export function PlatformConsole({
  user,
  onLogout,
}: {
  user: CurrentUser
  onLogout: () => Promise<void>
}) {
  const { t } = useTranslation('platform')
  const location = useLocation()
  const isTenantPage = location.pathname === '/platform/tenants'
  const isAuditPage = location.pathname === '/platform/audit'
  const isModelPage = location.pathname === '/platform/models'
  const isJobsPage = location.pathname === '/platform/jobs'

  return (
    <div className="platform-shell">
      <aside className="platform-sidebar">
        <Link className="brand" to="/platform" aria-label={t('brandHome')}>
          <span className="brand-mark"><Hexagon size={22} strokeWidth={2.2} /><span /></span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">PLATFORM</span>
        </Link>
        <div className="platform-identity">
          <span className="platform-identity-icon"><ShieldCheck size={18} /></span>
          <span><strong>{t('identityTitle')}</strong><small>{t('identitySub')}</small></span>
        </div>
        <div className="nav-caption">{t('overview')}</div>
        <nav className="primary-nav" aria-label={t('navLabel')}>
          <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} end to="/platform">
            <LayoutDashboard size={18} strokeWidth={1.8} /><span>{t('overviewPage')}</span>
          </NavLink>
        </nav>
        <div className="nav-caption nav-caption-lower">{t('opsData')}</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/tenants">
          <Building2 size={18} strokeWidth={1.8} /><span>{t('tenants')}</span>
        </NavLink>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/jobs">
          <ListRestart size={18} strokeWidth={1.8} /><span>{t('jobs')}</span>
        </NavLink>
        <div className="nav-caption">{t('definitions')}</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/models">
          <Boxes size={18} strokeWidth={1.8} /><span>{t('models')}</span>
        </NavLink>
        <div className="nav-caption">{t('auditSection')}</div>
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/audit">
          <FileText size={18} strokeWidth={1.8} /><span>{t('auditPage')}</span>
        </NavLink>
        <div className="platform-sidebar-note">
          <div className="section-kicker">{t('boundaryKicker')}</div>
          <p>{t('boundaryBody')}</p>
        </div>
        <div className="sidebar-spacer" />
        <button className="profile-button" onClick={() => void onLogout()} title={t('logoutTitle')} type="button">
          <span className="profile-avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
          <span className="profile-copy"><strong>{user.display_name}</strong><small>{t('adminRole')}</small></span>
          <LogOut size={15} />
        </button>
      </aside>
      <main className="main-area platform-main">
        <header className="topbar">
          <div className="breadcrumbs"><span>{t('breadcrumbRoot')}</span><span className="crumb-slash">/</span><strong>{isTenantPage ? t('tenants') : isAuditPage ? t('auditPage') : isModelPage ? t('models') : isJobsPage ? t('jobs') : t('overviewPage')}</strong></div>
          <div className="topbar-actions">
            <LanguageSwitch persist /><span className="platform-admin-badge"><i />{t('adminBadge')}</span>
            {user.memberships.length > 0 && <Link className="platform-tenant-link" to="/">{t('enterWorkspace')}</Link>}
          </div>
        </header>
        <div className="page-content platform-content">
          {isAuditPage
            ? <AuditLogPage key="platform-audit" canExport platformMode />
            : isModelPage
              ? <PlatformModelsPage />
              : isJobsPage
                ? <PlatformJobsPage />
              : isTenantPage
                ? <PlatformTenantsPage currentUserId={user.id} />
                : <PlatformOverviewPage />}
        </div>
      </main>
    </div>
  )
}
