import { useCallback, useEffect, useRef, useState } from 'react'
import { Bell } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../api'
import { formatRelativeTime } from '../i18n/format'
import { notificationTitle, safeInAppLink } from './notificationText'
import type { NotificationItem } from './notificationText'

const POLL_MS = 60000

export function NotificationBell({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation('notifications')
  const navigate = useNavigate()
  const [unread, setUnread] = useState(0)
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'all' | 'unread'>('all')
  const [items, setItems] = useState<NotificationItem[]>([])
  const [error, setError] = useState('')
  const box = useRef<HTMLSpanElement>(null)

  const loadCount = useCallback(() => {
    apiRequest<{ unread: number }>('/api/v1/notifications/unread-count', {}, tenantId)
      .then((result) => setUnread(result.unread))
      .catch(() => setUnread(0))
  }, [tenantId])

  useEffect(() => {
    loadCount()
    const timer = window.setInterval(loadCount, POLL_MS)
    return () => window.clearInterval(timer)
  }, [loadCount])

  useEffect(() => {
    if (!open) return
    let active = true
    apiRequest<{ items: NotificationItem[] }>(`/api/v1/notifications?limit=10${tab === 'unread' ? '&unread=true' : ''}`, {}, tenantId)
      .then((result) => { if (active) { setItems(result.items); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [open, tab, tenantId, t])

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  async function openItem(item: NotificationItem) {
    try {
      if (!item.read_at) await apiRequest(`/api/v1/notifications/${item.id}/read`, { method: 'POST' }, tenantId)
    } catch {
      // Navigating still works if marking read failed; the next poll corrects the count.
    }
    loadCount()
    setOpen(false)
    const link = safeInAppLink(item.link)
    if (link) navigate(link)
  }

  async function readAll() {
    try {
      await apiRequest('/api/v1/notifications/read-all', { method: 'POST' }, tenantId)
      setItems((current) => current.map((item) => ({ ...item, read_at: item.read_at ?? new Date().toISOString() })))
      setUnread(0)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  return (
    <span className="notification-wrap" ref={box}>
      <button aria-expanded={open} aria-haspopup="dialog" aria-label={unread > 0 ? t('bellUnread', { count: unread }) : t('bell')} className="icon-button notification-button" onClick={() => setOpen(!open)} type="button">
        <Bell size={18} />{unread > 0 && <i />}
      </button>
      {open && (
        <div aria-label={t('title')} className="notification-panel" role="dialog">
          <header>
            <div className="notification-tabs" role="tablist">
              <button aria-selected={tab === 'all'} className={tab === 'all' ? 'selected' : ''} onClick={() => setTab('all')} role="tab" type="button">{t('tabAll')}</button>
              <button aria-selected={tab === 'unread'} className={tab === 'unread' ? 'selected' : ''} onClick={() => setTab('unread')} role="tab" type="button">{t('tabUnread', { count: unread })}</button>
            </div>
            <button className="text-button" disabled={unread === 0} onClick={() => void readAll()} type="button">{t('readAll')}</button>
          </header>
          {error && <p className="form-error" role="alert">{error}</p>}
          {!error && items.length === 0 && <p className="wizard-muted">{tab === 'unread' ? t('emptyUnread') : t('empty')}</p>}
          <ul>
            {items.map((item) => (
              <li className={item.read_at ? '' : 'unread'} key={item.id}>
                <button onClick={() => void openItem(item)} type="button">
                  <strong>{notificationTitle(t, item)}</strong>
                  {item.body && <span>{item.body}</span>}
                  <small>{formatRelativeTime(item.created_at)}</small>
                </button>
              </li>
            ))}
          </ul>
          <footer><Link onClick={() => setOpen(false)} to="/notifications">{t('viewAll')}</Link></footer>
        </div>
      )}
    </span>
  )
}
