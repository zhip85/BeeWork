import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

type PlatformSkill = { id: string; slug: string; name: string; description: string; content: string; status: 'active' | 'disabled'; updated_at: string; updated_by: string | null }

/** Built-in skills (CAP-009): what every tenant sees as “平台可信 · Built-in”. Changes reach all tenants at once. */
export function PlatformSkillsPage() {
  const { t } = useTranslation('capabilities')
  const [rows, setRows] = useState<PlatformSkill[]>([])
  const [editing, setEditing] = useState<PlatformSkill | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  const load = () => apiRequest<PlatformSkill[]>('/api/v1/platform/skills').then(setRows).catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  useEffect(() => { void load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    if (!editing) return
    setSaving(true); setError('')
    try {
      await apiRequest(`/api/v1/platform/skills/${editing.id}`, { method: 'PUT', body: JSON.stringify({ name: editing.name, description: editing.description, content: editing.content, status: editing.status }) })
      setEditing(null); setNotice(t('saved')); void load()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
    setSaving(false)
  }

  return (
    <section className="knowledge-page">
      <div className="page-heading"><div><h1>{t('platformSkills.title')}</h1><p>{t('platformSkills.subtitle')}</p></div></div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="flows-grid">
        {rows.map((row) => (
          <article className="flows-card" key={row.id}>
            <header><span className={`assistant-status flows-status-${row.status === 'active' ? 'published' : 'disabled'}`}>{t(`platformSkills.status.${row.status}`)}</span></header>
            <div className="flows-card-body"><h3>{row.name}</h3><p>{row.description}</p></div>
            <footer><code>{row.slug}</code><button className="text-button" onClick={() => setEditing(row)} type="button">{t('menuConfigure')}</button></footer>
          </article>
        ))}
      </div>
      {editing && (
        <div className="modal-backdrop" onClick={() => setEditing(null)} role="presentation">
          <div aria-modal="true" className="create-modal portal-app-dialog" onClick={(event) => event.stopPropagation()} role="dialog">
            <h2>{editing.name}</h2>
            <label className="kb-field"><span>{t('form.name')}</span><input maxLength={50} onChange={(event) => setEditing({ ...editing, name: event.target.value })} value={editing.name} /></label>
            <label className="kb-field"><span>{t('form.description')}</span><textarea maxLength={500} onChange={(event) => setEditing({ ...editing, description: event.target.value })} rows={2} value={editing.description} /></label>
            <label className="kb-field"><span>SKILL.md</span><textarea className="mono" onChange={(event) => setEditing({ ...editing, content: event.target.value })} rows={10} value={editing.content} /></label>
            <label className="portal-featured"><input checked={editing.status === 'active'} onChange={(event) => setEditing({ ...editing, status: event.target.checked ? 'active' : 'disabled' })} type="checkbox" /> {t('platformSkills.activeLabel')}</label>
            <p className="wizard-muted">{t('platformSkills.note')}</p>
            <div className="confirm-actions">
              <button className="secondary-button" disabled={saving} onClick={() => setEditing(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving} onClick={() => void save()} type="button">{saving ? t('saving') : t('save')}</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
