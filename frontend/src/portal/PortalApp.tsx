import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronsUpDown, Hexagon, LayoutGrid, LogOut, Menu, MessageSquarePlus, Search, SquareArrowLeft, User, FolderKanban, X } from 'lucide-react'
import { Link, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../api'
import PortalNoSeat from './PortalNoSeat'
import { LanguageSwitch } from '../i18n/LanguageSwitch'
import type { CurrentUser, TenantMembership } from '../types'
import PortalHome from './PortalHome'
import type { PortalConfig, PortalSeat } from './portalTypes'
import './portal.css'

function Soon({ title }: { title: string }) {
  const { t } = useTranslation('portalApp')
  return (
    <section className="portal-soon">
      <h1>{title}</h1>
      <p>{t('sectionSoon')}</p>
    </section>
  )
}

/** The employee-side shell. Everything it shows is decided by the server's /portal/me/config. */
export default function PortalApp({ user, tenant, onLogout }: { user: CurrentUser; tenant: TenantMembership; onLogout: () => void }) {
  const { t } = useTranslation('portalApp')
  const [config, setConfig] = useState<PortalConfig | null>(null)
  const [seat, setSeat] = useState<PortalSeat | null>(null)
  const [seatNotice, setSeatNotice] = useState(false)
  const [error, setError] = useState('')
  const [drawer, setDrawer] = useState(false)
  const [menu, setMenu] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const location = useLocation()
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    apiRequest<PortalConfig>('/api/v1/portal/me/config', {}, tenant.id)
      .then((result) => { if (active) { setConfig(result); setError('') } })
      .catch((failure: Error) => { if (active) setError(failure.message) })
    return () => { active = false }
  }, [tenant.id, attempt])

  // Entering the portal asks the server whether this member may use it (and records the sign-in / auto-assigns a seat).
  const loadSeat = useCallback(() => {
    apiRequest<PortalSeat>('/api/v1/portal/me/seat', {}, tenant.id)
      .then((result) => { setSeat(result); if (result.auto_assigned) setSeatNotice(true) })
      .catch((failure: Error) => setError(failure.message))
  }, [tenant.id])
  useEffect(() => { loadSeat() }, [loadSeat, attempt])
  // A request waiting for approval is picked up without the employee having to reload.
  useEffect(() => {
    if (seat?.state !== 'requested') return
    const timer = window.setInterval(loadSeat, 20000)
    return () => window.clearInterval(timer)
  }, [seat?.state, loadSeat])

  async function requestSeat(reason: string) {
    const result = await apiRequest<PortalSeat>('/api/v1/portal/me/seat/request', { method: 'POST', body: JSON.stringify({ reason }) }, tenant.id)
    setSeat(result)
  }

  useEffect(() => { setDrawer(false); setMenu(false) }, [location.pathname])
  useEffect(() => {
    if (!menu) return
    const close = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [menu])

  if (error) {
    return (
      <main className="portal portal-center">
        <h1>{t('configFailed')}</h1>
        <p role="alert">{error}</p>
        <button className="portal-primary" onClick={() => { setError(''); setAttempt((value) => value + 1) }} type="button">{t('retry')}</button>
      </main>
    )
  }
  if (!config || !seat) return <main className="portal portal-center"><p>{t('loading')}</p></main>
  if (seat.state !== 'assigned') {
    return <PortalNoSeat name={seat.portal_name} onLogout={onLogout} onRefresh={loadSeat} onRequest={requestSeat} requested={seat.state === 'requested'} />
  }

  const name = config.portal_name || t('defaultName')
  const link = ({ isActive }: { isActive: boolean }) => `portal-nav-link${isActive ? ' active' : ''}`

  return (
    <div className="portal portal-shell">
      <header className="portal-mobilebar">
        <button aria-label={t('openMenu')} onClick={() => setDrawer(true)} type="button"><Menu size={20} /></button>
        <strong>{name}</strong>
      </header>
      {drawer && <div className="portal-scrim" onClick={() => setDrawer(false)} />}
      <aside className={`portal-sidebar${drawer ? ' open' : ''}`}>
        <div className="portal-sidebar-head">
          <Link className="portal-brand" to="/portal"><span className="portal-mark"><Hexagon size={18} strokeWidth={2.2} /></span><strong>{name}</strong></Link>
          <button aria-label={t('closeMenu')} className="portal-drawer-close" onClick={() => setDrawer(false)} type="button"><X size={18} /></button>
        </div>
        <nav aria-label={name} className="portal-nav">
          <NavLink className={link} end to="/portal"><MessageSquarePlus size={16} />{t('newChat')}</NavLink>
          {config.nav_search && <NavLink className={link} to="/portal/search"><Search size={16} />{t('search')}</NavLink>}
          {config.nav_projects && <NavLink className={link} to="/portal/projects"><FolderKanban size={16} />{t('projects')}</NavLink>}
          {config.nav_app_library && <NavLink className={link} to="/portal/apps"><LayoutGrid size={16} />{t('apps')}</NavLink>}
        </nav>
        <div className="portal-history">
          {config.nav_pinned && <div className="portal-history-group"><small>{t('pinned')}</small><span>{t('noneYet')}</span></div>}
          {config.nav_recent && <div className="portal-history-group"><small>{t('recent')}</small><span>{t('noneYet')}</span></div>}
        </div>
        <div className="portal-account" ref={menuRef}>
          {menu && (
            <div className="portal-menu" role="menu">
              <Link role="menuitem" to="/portal/profile"><User size={15} />{t('profile')}</Link>
              {config.has_console && <Link role="menuitem" to="/"><SquareArrowLeft size={15} />{t('backToConsole')}</Link>}
              <button onClick={onLogout} role="menuitem" type="button"><LogOut size={15} />{t('logout')}</button>
            </div>
          )}
          <button aria-expanded={menu} aria-haspopup="menu" aria-label={t('account')} className="portal-account-button" onClick={() => setMenu((value) => !value)} type="button">
            <span className="portal-avatar">{user.display_name.slice(0, 1).toUpperCase()}</span>
            <span className="portal-account-copy"><strong>{user.display_name}</strong><small>{tenant.name}</small></span>
            <ChevronsUpDown size={14} />
          </button>
          <LanguageSwitch persist />
        </div>
      </aside>
      <main className="portal-main">
        {seatNotice && (
          <p className="portal-banner" role="status">
            {t('noSeat.autoAssigned')}
            <button aria-label={t('noSeat.dismiss')} onClick={() => setSeatNotice(false)} type="button"><X size={14} /></button>
          </p>
        )}
        <Routes>
          <Route path="/portal" element={<PortalHome config={config} name={user.display_name} />} />
          {config.nav_search && <Route path="/portal/search" element={<Soon title={t('search')} />} />}
          {config.nav_projects && <Route path="/portal/projects" element={<Soon title={t('projects')} />} />}
          {config.nav_app_library && <Route path="/portal/apps" element={<Soon title={t('apps')} />} />}
          <Route path="/portal/profile" element={<Soon title={t('profile')} />} />
          <Route path="/portal/*" element={<Navigate replace to="/portal" />} />
        </Routes>
      </main>
    </div>
  )
}
