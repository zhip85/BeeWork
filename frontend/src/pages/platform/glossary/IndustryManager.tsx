import { useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../../api'
import type { Industry } from './types'

/** The unified industry dictionary (PRD 8.3 #6): add, rename, disable; an industry in use cannot be deleted. */
export function IndustryManager({ industries, onChanged, onClose }: { industries: Industry[]; onChanged: () => void; onClose: () => void }) {
  const { t } = useTranslation('glossary')
  const [error, setError] = useState('')
  const [draft, setDraft] = useState({ code: '', name_zh: '', name_en: '' })
  const [editing, setEditing] = useState<Industry | null>(null)

  async function run(action: () => Promise<unknown>) {
    setError('')
    try {
      await action()
      onChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  const create = () => run(async () => {
    await apiRequest('/api/v1/platform/industries', { method: 'POST', body: JSON.stringify({ ...draft, sort_order: industries.length }) })
    setDraft({ code: '', name_zh: '', name_en: '' })
  })
  const save = () => run(async () => {
    if (!editing) return
    await apiRequest(`/api/v1/platform/industries/${editing.code}`, { method: 'PUT', body: JSON.stringify({ name_zh: editing.name_zh, name_en: editing.name_en, sort_order: editing.sort_order, status: editing.status }) })
    setEditing(null)
  })
  const toggle = (industry: Industry) => run(() => apiRequest(`/api/v1/platform/industries/${industry.code}`, {
    method: 'PUT', body: JSON.stringify({ name_zh: industry.name_zh, name_en: industry.name_en, sort_order: industry.sort_order, status: industry.status === 'active' ? 'disabled' : 'active' }),
  }))
  const remove = (industry: Industry) => run(() => apiRequest(`/api/v1/platform/industries/${industry.code}`, { method: 'DELETE' }))

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div aria-modal="true" className="create-modal governance-modal industry-manager" onClick={(event) => event.stopPropagation()} role="dialog">
        <button aria-label={t('cancel')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <div className="section-kicker">PLATFORM</div>
        <h2>{t('industries.title')}</h2>
        <p className="usage-hint">{t('industries.hint')}</p>
        <table className="members-table">
          <thead><tr><th>{t('industries.code')}</th><th>{t('industries.name')}</th><th>{t('industries.usage')}</th><th>{t('industries.status')}</th><th /></tr></thead>
          <tbody>
            {industries.map((industry) => (
              <tr className={industry.status === 'active' ? '' : 'governance-off'} key={industry.code}>
                <td><code className="governance-pattern">{industry.code}</code></td>
                <td>
                  {editing?.code === industry.code
                    ? <span className="industry-edit"><input aria-label={t('industries.nameZh')} onChange={(event) => setEditing({ ...editing, name_zh: event.target.value })} value={editing.name_zh} /><input aria-label={t('industries.nameEn')} onChange={(event) => setEditing({ ...editing, name_en: event.target.value })} value={editing.name_en} /></span>
                    : <><strong>{industry.name_zh}</strong><small>{industry.name_en}</small></>}
                </td>
                <td>{t('industries.usageValue', { tenants: industry.tenant_count, glossaries: industry.glossary_count })}</td>
                <td><span className={`assistant-status ${industry.status === 'active' ? 'published' : 'draft'}`}>{t(`industries.${industry.status}`)}</span></td>
                <td className="member-actions-cell">
                  {editing?.code === industry.code
                    ? <><button className="text-button" onClick={() => void save()} type="button">{t('save')}</button><button className="text-button" onClick={() => setEditing(null)} type="button">{t('cancel')}</button></>
                    : <>
                      <button className="text-button" onClick={() => setEditing(industry)} type="button">{t('rename')}</button>
                      {industry.code !== 'general_office' && <button className="text-button" onClick={() => void toggle(industry)} type="button">{t(industry.status === 'active' ? 'industries.disable' : 'industries.enable')}</button>}
                      {industry.code !== 'general_office' && industry.tenant_count === 0 && industry.glossary_count === 0 && <button className="text-button" onClick={() => void remove(industry)} type="button">{t('delete')}</button>}
                    </>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form className="industry-add" onSubmit={(event) => { event.preventDefault(); void create() }}>
          <input aria-label={t('industries.code')} onChange={(event) => setDraft({ ...draft, code: event.target.value.toLowerCase() })} pattern="[a-z][a-z0-9_]{1,30}" placeholder="code" required value={draft.code} />
          <input aria-label={t('industries.nameZh')} onChange={(event) => setDraft({ ...draft, name_zh: event.target.value })} placeholder={t('industries.nameZh')} required value={draft.name_zh} />
          <input aria-label={t('industries.nameEn')} onChange={(event) => setDraft({ ...draft, name_en: event.target.value })} placeholder={t('industries.nameEn')} required value={draft.name_en} />
          <button className="primary-button" type="submit">{t('industries.add')}</button>
        </form>
        {error && <p className="members-error" role="alert">{error}</p>}
      </div>
    </div>
  )
}
