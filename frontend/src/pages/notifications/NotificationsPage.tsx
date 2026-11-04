import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { apiRequest } from '../../api'
import { notificationTitle, safeInAppLink } from '../../components/notificationText'
import type { NotificationItem } from '../../components/notificationText'
import { formatRelativeTime } from '../../i18n/format'

const PAGE_SIZE = 20

export function NotificationsPage({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation('notifications')
  const navigate = useNavigate()
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<{ items: NotificationItem[]; total: number } | null>(null)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let active = true
    apiRequest<{ items: NotificationItem[]; total: number }>(`/api/v1/notifications?limit=${PAGE_SIZE}&offset=${offset}${unreadOnly ? '&unread=true' : ''}`, {}, tenantId)
      .then((result) => { if (active) { setData(result); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [tenantId, unreadOnly, offset, reload, t])

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  async function open(item: NotificationItem) {
    try {
      if (!item.read_at) await apiRequest(`/api/v1/notifications/${item.id}/read`, { method: 'POST' }, tenantId)
    } catch {
      // Still navigate; the list refreshes on return.
    }
    const link = safeInAppLink(item.link)
    if (link) navigate(link)
    else refresh()
  }

  async function readAll() {
    try {
      await apiRequest('/api/v1/notifications/read-all', { method: 'POST' }, tenantId)
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  return (
    <section className="settings-page">
      <div className="page-heading"><div><h1>{t('title')}</h1><p>{t('subtitle')}</p></div>
        <button className="secondary-button" onClick={() => void readAll()} type="button">{t('readAll')}</button>
      </div>
      <div className="assistants-toolbar">
        <label><input checked={unreadOnly} onChange={(event) => { setUnreadOnly(event.target.checked); setOffset(0) }} type="checkbox" /> {t('unreadOnly')}</label>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {!data && !error && <p>{t('loading')}</p>}
      {data && data.items.length === 0 && <p className="assistants-empty-filtered">{unreadOnly ? t('emptyUnread') : t('empty')}</p>}
      {data && data.items.length > 0 && (
        <ul className="notification-list">
          {data.items.map((item) => (
            <li className={item.read_at ? '' : 'unread'} key={item.id}>
              <button onClick={() => void open(item)} type="button">
                <strong>{notificationTitle(t, item)}</strong>
                {item.body && <span>{item.body}</span>}
                <small>{formatRelativeTime(item.created_at)}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data && data.total > PAGE_SIZE && (
        <div className="confirm-actions">
          <button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} type="button">{t('prev')}</button>
          <small>{t('range', { from: offset + 1, to: Math.min(offset + PAGE_SIZE, data.total), total: data.total })}</small>
          <button className="secondary-button" disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)} type="button">{t('next')}</button>
        </div>
      )}
    </section>
  )
}
