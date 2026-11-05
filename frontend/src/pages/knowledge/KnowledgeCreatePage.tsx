import { useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { KbType, KnowledgeBase } from './types'

export function KnowledgeCreatePage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('knowledge')
  const navigate = useNavigate()
  const [kbType, setKbType] = useState<KbType>('standard')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [language, setLanguage] = useState('multilingual')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const created = await apiRequest<KnowledgeBase>('/api/v1/knowledge-bases', {
        method: 'POST',
        body: JSON.stringify({ kb_type: kbType, name: name.trim(), description, language }),
      }, tenant.id)
      navigate(`/knowledge/${created.id}`, { state: { notice: t('createdNotice') } })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setSaving(false)
    }
  }

  return (
    <section className="knowledge-page">
      <Link className="text-button" to="/knowledge"><ArrowLeft size={14} /> {t('backToList')}</Link>
      <div className="page-heading"><div><small>KNOWLEDGE</small><h1>{t('createTitle')}</h1><p>{t('createSubtitle')}</p></div></div>
      <form className="detail-card knowledge-form" onSubmit={(event) => void submit(event)}>
        <h2>{t('basics')}</h2>
        <fieldset className="knowledge-type-choice"><legend>{t('typeLabel')}</legend>
          {(['standard', 'faq'] as const).map((value) => (
            <label className={`knowledge-type-card${kbType === value ? ' selected' : ''}`} key={value}>
              <input checked={kbType === value} name="kb_type" onChange={() => setKbType(value)} type="radio" />
              <strong>{t(`type.${value}`)}</strong><small>{t(`typeHint.${value}`)}</small>
            </label>
          ))}
        </fieldset>
        <label>{t('name')}
          <input autoFocus maxLength={50} onChange={(event) => setName(event.target.value)} placeholder={t('namePlaceholder')} required value={name} />
        </label>
        <label>{t('description')}
          <textarea maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder={t('descriptionPlaceholder')} rows={3} value={description} />
        </label>
        <label>{t('languageLabel')}
          <select onChange={(event) => setLanguage(event.target.value)} value={language}>
            {(['multilingual', 'zh-CN', 'en'] as const).map((value) => <option key={value} value={value}>{t(`language.${value}`)}</option>)}
          </select>
        </label>
        <p className="usage-hint">{t(`createHint.${kbType}`)}</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="plans-actions">
          <Link className="text-button" to="/knowledge">{t('cancel')}</Link>
          <button className="primary-button" disabled={saving || !name.trim()} type="submit">{saving ? t('creating') : t('createAndContinue')}</button>
        </div>
      </form>
    </section>
  )
}
