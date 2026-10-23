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
  const location = useLocation()
  const isTenantPage = location.pathname === '/platform/tenants'
  const isAuditPage = location.pathname === '/platform/audit'
  const isModelPage = location.pathname === '/platform/models'
  const isJobsPage = location.pathname === '/platform/jobs'

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
        <NavLink className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} to="/platform/jobs">
          <ListRestart size={18} strokeWidth={1.8} /><span>后台任务</span>
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
          <div className="breadcrumbs"><span>BeeWork 平台</span><span className="crumb-slash">/</span><strong>{isTenantPage ? '租户管理' : isAuditPage ? '操作审计' : isModelPage ? '模型目录' : isJobsPage ? '后台任务' : '平台总览'}</strong></div>
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
