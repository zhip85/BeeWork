import { useCallback, useEffect, useState } from 'react'
import { BookOpen, MessageCircleQuestion, MoreHorizontal, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import type { KnowledgeBase, KnowledgePage } from './types'

const TINTS = ['violet', 'blue', 'green', 'orange'] as const

export function KnowledgeListPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('knowledge')
  const navigate = useNavigate()
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && tenant.permissions.includes('knowledge:edit')
  const canDelete = writable && tenant.permissions.includes('knowledge:delete')
  const [items, setItems] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<{ kb: KnowledgeBase; references: number } | null>(null)

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: '100' })
    if (search.trim()) params.set('q', search.trim())
    apiRequest<KnowledgePage>(`/api/v1/knowledge-bases?${params}`, {}, tenant.id)
      .then((page) => { setItems(page.items); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [search, tenant.id, t])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  useEffect(() => {
    if (!menuFor) return
    const close = () => setMenuFor(null)
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('click', close)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', onKey) }
  }, [menuFor])

  const open = (kb: KnowledgeBase) => navigate(`/knowledge/${kb.id}`)

  async function copy(kb: KnowledgeBase) {
    setMenuFor(null)
    try {
      await apiRequest(`/api/v1/knowledge-bases/${kb.id}/copy`, { method: 'POST' }, tenant.id)
      setNotice(t('copied'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function askDelete(kb: KnowledgeBase) {
    setMenuFor(null)
    try {
      const refs = await apiRequest<{ count: number }>(`/api/v1/knowledge-bases/${kb.id}/references`, {}, tenant.id)
      setDeleting({ kb, references: refs.count })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function confirmDelete() {
    if (!deleting) return
    try {
      await apiRequest(`/api/v1/knowledge-bases/${deleting.kb.id}`, { method: 'DELETE' }, tenant.id)
      setNotice(t('deleted'))
      load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setDeleting(null)
  }

  return (
    <section className="knowledge-page">
      <div className="page-heading">
        <div><h1>{t('title')}</h1><p>{t('subtitle')}</p></div>
        {canEdit && <Link className="primary-button" to="/knowledge/new"><Plus size={16} /> {t('create')}</Link>}
      </div>
      <div className="knowledge-toolbar">
        <label className="search-field"><Search size={15} /><input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></label>
        <span>{t('count', { count: items.length })}</span>
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <p>{t('loading')}</p>}
      <div className="knowledge-grid">
        {items.map((kb, index) => (
          <article className="knowledge-card" key={kb.id}>
            <header>
              <span className={`knowledge-icon ${kb.kb_type === 'faq' ? 'cyan' : TINTS[index % TINTS.length]}`}>
                {kb.kb_type === 'faq' ? <MessageCircleQuestion size={18} /> : <BookOpen size={18} />}
              </span>
              <span className={`assistant-status knowledge-status-${kb.status}`}>{t(`status.${kb.status}`)}</span>
              {(canEdit || canDelete) && (
                <span className="assistant-menu">
                  <button aria-expanded={menuFor === kb.id} aria-haspopup="menu" aria-label={t('menu')} className="icon-button" onClick={(event) => { event.stopPropagation(); setMenuFor(menuFor === kb.id ? null : kb.id) }} type="button"><MoreHorizontal size={16} /></button>
                  {menuFor === kb.id && (
                    <ul role="menu">
                      <li role="none"><Link role="menuitem" to={`/knowledge/${kb.id}?view=settings`}>{t('menuSettings')}</Link></li>
                      {canEdit && <li role="none"><button onClick={() => void copy(kb)} role="menuitem" type="button">{t('menuCopy')}</button></li>}
                      {canDelete && <li role="none"><button className="danger" onClick={() => void askDelete(kb)} role="menuitem" type="button">{t('menuDelete')}</button></li>}
                    </ul>
                  )}
                </span>
              )}
            </header>
            <button className="knowledge-card-body" onClick={() => open(kb)} type="button">
              <small>{t(`type.${kb.kb_type}`)}</small>
              <h3>{kb.name}</h3>
              <p>{kb.description || t('defaultDescription')}</p>
            </button>
            <footer>
              <span>{kb.kb_type === 'faq' ? t('statsFaq', { faqs: kb.faq_count }) : t('stats', { docs: kb.docs_count, faqs: kb.faq_count })}</span>
              <span>{t(`language.${kb.language}`)}</span>
            </footer>
            {kb.sync_status !== 'synced' && (
              <small className={`knowledge-sync ${kb.sync_status}`}>{t(`sync.${kb.sync_status}`)}</small>
            )}
          </article>
        ))}
        {canEdit && (
          <Link className="knowledge-card knowledge-card-new" to="/knowledge/new">
            <Plus size={20} /><strong>{t('create')}</strong><small>{t('newHint')}</small>
          </Link>
        )}
      </div>
      {!loading && items.length === 0 && !canEdit && <p className="assistants-empty-filtered">{t('empty')}</p>}
      {deleting && (
        <ConfirmDialog
          body={deleting.references > 0 ? t('deleteBodyReferenced', { count: deleting.references }) : t('deleteBody')}
          danger
          onCancel={() => setDeleting(null)}
          onConfirm={() => confirmDelete()}
          title={t('deleteTitle')}
        />
      )}
    </section>
  )
}
