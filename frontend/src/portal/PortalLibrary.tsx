import { useEffect, useState } from 'react'
import { Search, Sparkles, Star } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { usePortal } from './portalContext'
import type { PortalAppEntry } from './portalTypes'

/** The AI app library (演进 PRD 11.4): what this employee may use, searchable by name / description / category.
 * The server decides visibility; entering an app opens a conversation with it. */
export function PortalLibrary() {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const [q, setQ] = useState('')
  const [category, setCategory] = useState('')
  const [items, setItems] = useState<PortalAppEntry[]>([])
  const [categories, setCategories] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const request = portal.request

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams()
      if (q.trim()) params.set('q', q.trim())
      if (category) params.set('category', category)
      request<{ items: PortalAppEntry[]; categories: string[] }>(`/api/v1/portal/me/app-library?${params}`)
        .then((result) => { setItems(result.items); setCategories(result.categories); setError(''); setLoaded(true) })
        .catch((failure: Error) => { setError(failure.message); setLoaded(true) })
    }, 200)
    return () => window.clearTimeout(timer)
  }, [q, category, request])

  const filtering = Boolean(q.trim() || category)
  return (
    <section className="portal-page portal-library">
      <h1>{t('apps')}</h1>
      <label className="portal-search"><Search size={15} /><input aria-label={t('library.search')} onChange={(event) => setQ(event.target.value)} placeholder={t('library.search')} value={q} /></label>
      {categories.length > 0 && (
        <div className="portal-chips" role="group" aria-label={t('library.categories')}>
          <button aria-pressed={category === ''} className={category === '' ? 'selected' : ''} onClick={() => setCategory('')} type="button">{t('library.all')}</button>
          {categories.map((name) => <button aria-pressed={category === name} className={category === name ? 'selected' : ''} key={name} onClick={() => setCategory(category === name ? '' : name)} type="button">{name}</button>)}
        </div>
      )}
      {error && <p className="portal-error" role="alert">{error}</p>}
      {loaded && !error && items.length === 0 && <p className="portal-hint" role="status">{filtering ? t('library.noMatch') : t('library.empty')}</p>}
      <ul className="portal-cards">
        {items.map((app) => (
          <li key={app.id}>
            <Link to={`/portal?app=${app.id}`}>
              <span className="portal-app-icon"><Sparkles size={16} /></span>
              <strong>{app.name}{app.featured && <span className="portal-featured"><Star size={11} /> {t('library.featured')}</span>}</strong>
              <small>{app.description || t('projects.noDescription')}</small>
              {app.category && <small className="portal-tag">{app.category}</small>}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}
