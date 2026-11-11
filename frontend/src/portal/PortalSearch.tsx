import { useEffect, useState } from 'react'
import { Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import PortalConversationItem from './PortalConversationItem'
import { usePortal } from './portalContext'
import type { PortalConversation } from './portalTypes'

/** Search the employee's own conversations by title. */
export function PortalSearch() {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const [q, setQ] = useState('')
  const [items, setItems] = useState<PortalConversation[]>([])
  const [error, setError] = useState('')
  const request = portal.request

  useEffect(() => {
    const timer = window.setTimeout(() => {
      request<{ items: PortalConversation[] }>(`/api/v1/portal/me/conversations?${new URLSearchParams(q.trim() ? { q: q.trim() } : {})}`)
        .then((result) => { setItems(result.items); setError('') })
        .catch((failure: Error) => setError(failure.message))
    }, 200)
    return () => window.clearTimeout(timer)
  }, [q, request, portal.conversations])

  return (
    <section className="portal-page">
      <h1>{t('search')}</h1>
      <label className="portal-search"><Search size={15} /><input aria-label={t('searchPlaceholder')} autoFocus onChange={(event) => setQ(event.target.value)} placeholder={t('searchPlaceholder')} value={q} /></label>
      {error && <p className="portal-error" role="alert">{error}</p>}
      {items.length === 0 ? <p className="portal-hint">{q.trim() ? t('searchNone') : t('noneYet')}</p> : <div className="portal-conv-list">{items.map((c) => <PortalConversationItem conversation={c} key={c.id} />)}</div>}
    </section>
  )
}
