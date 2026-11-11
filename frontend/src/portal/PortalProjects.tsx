import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import PortalChat from './PortalChat'
import PortalConversationItem from './PortalConversationItem'
import { usePortal } from './portalContext'
import type { PortalConversation, PortalProject } from './portalTypes'

/** The employee's projects: a private folder of conversations (演进 PRD 11.3). */
export function PortalProjects() {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')

  async function create(event: FormEvent) {
    event.preventDefault()
    try {
      await portal.request('/api/v1/portal/me/projects', { method: 'POST', body: JSON.stringify({ name, description }) })
      setName(''); setDescription(''); setError('')
      portal.reload()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('project.failed'))
    }
  }
  return (
    <section className="portal-page">
      <h1>{t('projects')}</h1>
      <form className="portal-form-row" onSubmit={(event) => void create(event)}>
        <input aria-label={t('project.name')} maxLength={60} onChange={(event) => setName(event.target.value)} placeholder={t('project.name')} required value={name} />
        <input aria-label={t('project.description')} maxLength={300} onChange={(event) => setDescription(event.target.value)} placeholder={t('project.description')} value={description} />
        <button className="portal-primary" disabled={!name.trim()} type="submit">{t('project.create')}</button>
      </form>
      {error && <p className="portal-error" role="alert">{error}</p>}
      {portal.projects.length === 0 ? <p className="portal-hint">{t('project.empty')}</p> : (
        <ul className="portal-cards">
          {portal.projects.map((project) => (
            <li key={project.id}>
              <Link to={`/portal/projects/${project.id}`}><strong>{project.name}</strong><small>{project.description || t('project.noDescription')}</small><small>{t('project.count', { count: project.conversation_count })}</small></Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export function PortalProjectDetail() {
  const { t } = useTranslation('portalApp')
  const { projectId = '' } = useParams()
  const portal = usePortal()
  const navigate = useNavigate()
  const [project, setProject] = useState<(PortalProject & { conversations: PortalConversation[] }) | null>(null)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const request = portal.request

  const load = useCallback(() => {
    request<PortalProject & { conversations: PortalConversation[] }>(`/api/v1/portal/me/projects/${projectId}`)
      .then((result) => { setProject(result); setName(result.name); setDescription(result.description); setError('') })
      .catch((failure: Error) => setError(failure.message))
  }, [projectId, request])
  // Reload when the shell's conversation list changes (a new chat in this project, a move, a delete).
  useEffect(() => { load() }, [load, portal.conversations])

  async function save(event: FormEvent) {
    event.preventDefault()
    try {
      await request(`/api/v1/portal/me/projects/${projectId}`, { method: 'PATCH', body: JSON.stringify({ name, description }) })
      setEditing(false); portal.reload(); load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('project.failed'))
    }
  }
  async function remove() {
    try {
      await request(`/api/v1/portal/me/projects/${projectId}`, { method: 'DELETE' })
      portal.reload(); navigate('/portal/projects')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('project.failed'))
    }
  }

  if (error && !project) return <section className="portal-page"><p className="portal-error" role="alert">{error}</p><Link className="portal-link" to="/portal/projects">{t('project.back')}</Link></section>
  if (!project) return <section className="portal-page"><p>{t('loading')}</p></section>
  return (
    <section className="portal-page">
      <Link className="portal-link" to="/portal/projects">{t('project.back')}</Link>
      {editing ? (
        <form className="portal-form-row" onSubmit={(event) => void save(event)}>
          <input aria-label={t('project.name')} maxLength={60} onChange={(event) => setName(event.target.value)} required value={name} />
          <input aria-label={t('project.description')} maxLength={300} onChange={(event) => setDescription(event.target.value)} value={description} />
          <button className="portal-primary" disabled={!name.trim()} type="submit">{t('project.save')}</button>
          <button className="portal-link portal-linkbutton" onClick={() => setEditing(false)} type="button">{t('conv.cancel')}</button>
        </form>
      ) : (
        <header className="portal-project-head">
          <div><h1>{project.name}</h1><p>{project.description}</p></div>
          <div className="portal-project-actions">
            <button className="portal-link portal-linkbutton" onClick={() => setEditing(true)} type="button">{t('project.edit')}</button>
            {confirmDelete ? (
              <><small className="portal-hint">{t('project.deleteConfirm')}</small><button className="portal-link portal-linkbutton danger" onClick={() => void remove()} type="button">{t('conv.deleteYes')}</button></>
            ) : <button className="portal-link portal-linkbutton danger" onClick={() => setConfirmDelete(true)} type="button">{t('conv.delete')}</button>}
          </div>
        </header>
      )}
      {error && <p className="portal-error" role="alert">{error}</p>}
      <h2>{t('project.newChat')}</h2>
      <PortalChat key={`new-${projectId}`} projectId={projectId} />
      <h2>{t('project.conversations')}</h2>
      {project.conversations.length === 0 ? <p className="portal-hint">{t('noneYet')}</p> : <div className="portal-conv-list">{project.conversations.map((c) => <PortalConversationItem conversation={c} key={c.id} />)}</div>}
    </section>
  )
}
