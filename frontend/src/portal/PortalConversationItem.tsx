import { useEffect, useRef, useState } from 'react'
import { MoreHorizontal, Pin } from 'lucide-react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { usePortal } from './portalContext'
import type { PortalConversation } from './portalTypes'

/** A conversation row in the sidebar / lists, with rename, pin, move-to-project and delete. */
export default function PortalConversationItem({ conversation }: { conversation: PortalConversation }) {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'menu' | 'projects' | 'delete'>('menu')
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(conversation.title)
  const [error, setError] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => { if (!ref.current?.contains(event.target as Node)) { setOpen(false); setMode('menu') } }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  async function change(body: Record<string, unknown>) {
    try {
      await portal.request(`/api/v1/portal/me/conversations/${conversation.id}`, { method: 'PATCH', body: JSON.stringify(body) })
      setError('')
      portal.reload()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('conv.failed'))
    }
    setOpen(false); setMode('menu'); setRenaming(false)
  }
  async function remove() {
    try {
      await portal.request(`/api/v1/portal/me/conversations/${conversation.id}`, { method: 'DELETE' })
      portal.reload()
      if (window.location.pathname === `/portal/c/${conversation.id}`) navigate('/portal')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('conv.failed'))
    }
    setOpen(false); setMode('menu')
  }

  if (renaming) {
    return (
      <form className="portal-conv-rename" onSubmit={(event) => { event.preventDefault(); if (title.trim()) void change({ title: title.trim() }) }}>
        <input aria-label={t('conv.rename')} autoFocus maxLength={120} onBlur={() => setRenaming(false)} onChange={(event) => setTitle(event.target.value)} value={title} />
      </form>
    )
  }
  return (
    <div className="portal-conv" ref={ref}>
      <NavLink className={({ isActive }) => `portal-conv-link${isActive ? ' active' : ''}`} to={`/portal/c/${conversation.id}`} title={conversation.title}>
        {conversation.pinned && <Pin size={12} aria-label={t('conv.pinned')} />}
        <span>{conversation.title}</span>
      </NavLink>
      <button aria-expanded={open} aria-label={t('conv.actions')} className="portal-conv-more" onClick={() => { setOpen((value) => !value); setMode('menu') }} type="button"><MoreHorizontal size={15} /></button>
      {error && <small className="portal-error" role="alert">{error}</small>}
      {open && (
        <div className="portal-menu portal-conv-menu" role="menu">
          {mode === 'menu' && (
            <>
              <button onClick={() => { setRenaming(true); setOpen(false) }} role="menuitem" type="button">{t('conv.rename')}</button>
              {portal.config.nav_pinned && <button onClick={() => void change({ pinned: !conversation.pinned })} role="menuitem" type="button">{conversation.pinned ? t('conv.unpin') : t('conv.pin')}</button>}
              {portal.config.nav_projects && <button onClick={() => setMode('projects')} role="menuitem" type="button">{t('conv.moveToProject')}</button>}
              <button className="danger" onClick={() => setMode('delete')} role="menuitem" type="button">{t('conv.delete')}</button>
            </>
          )}
          {mode === 'projects' && (
            <>
              {portal.projects.length === 0 && <small className="portal-hint">{t('conv.noProjects')}</small>}
              {portal.projects.map((project) => <button key={project.id} onClick={() => void change({ project_id: project.id })} role="menuitem" type="button">{project.name}</button>)}
              {conversation.project_id && <button onClick={() => void change({ clear_project: true })} role="menuitem" type="button">{t('conv.removeFromProject')}</button>}
            </>
          )}
          {mode === 'delete' && (
            <>
              <small className="portal-hint">{t('conv.deleteConfirm')}</small>
              <button className="danger" onClick={() => void remove()} role="menuitem" type="button">{t('conv.deleteYes')}</button>
              <button onClick={() => setMode('menu')} role="menuitem" type="button">{t('conv.cancel')}</button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
