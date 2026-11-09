import { useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft, ArrowRight, BookOpen, Check, CircleHelp } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { KbType, KnowledgeBase } from './types'

const TYPES = [
  { value: 'standard', icon: BookOpen, tone: 'violet' },
  { value: 'faq', icon: CircleHelp, tone: 'amber' },
] as const

/** Create knowledge base (KNW-010–013), laid out like the prototype's create page. */
export function KnowledgeCreatePage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('knowledge')
  const navigate = useNavigate()
  const [kbType, setKbType] = useState<KbType>('standard')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [language, setLanguage] = useState('multilingual')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const NoteIcon = TYPES.find((item) => item.value === kbType)?.icon ?? BookOpen

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
    <section className="kb-create-page">
      <Link className="kb-create-back" to="/knowledge"><ArrowLeft size={15} /> {t('backToList')}</Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <span className="kb-icon violet"><BookOpen size={24} /></span>
          <div>
            <span className="eyebrow">KNOWLEDGE</span>
            <h1>{t('createTitle')}</h1>
            <p>{t('createSubtitle')}</p>
          </div>
        </header>
        <form className="kb-create-form" onSubmit={(event) => void submit(event)}>
          <div className="kb-form-heading">
            <span>01</span>
            <div><h2>{t('basicsTitle')}</h2><p>{t('basicsSubtitle')}</p></div>
          </div>
          <fieldset className="kb-type-field">
            <legend>{t('typeLabel')} <em>*</em></legend>
            <div className="kb-type-picker" role="radiogroup">
              {TYPES.map(({ value, icon: Icon, tone }) => (
                <label className={kbType === value ? 'selected' : ''} key={value}>
                  <input checked={kbType === value} name="kb_type" onChange={() => setKbType(value)} type="radio" />
                  <span className={`kb-icon ${tone}`}><Icon size={19} /></span>
                  <span className="kb-type-text"><strong>{t(`type.${value}`)}</strong><small>{t(`typeHint.${value}`)}</small></span>
                  {kbType === value && <Check className="kb-type-check" size={16} />}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="kb-field">
            <span>{t('name')} <em>*</em></span>
            <input autoFocus maxLength={50} onChange={(event) => setName(event.target.value)} placeholder={t('namePlaceholder')} required value={name} />
          </label>
          <label className="kb-field">
            <span>{t('description')}</span>
            <textarea maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder={t('descriptionPlaceholder')} rows={3} value={description} />
          </label>
          <label className="kb-field">
            <span>{t('languageLabel')}</span>
            <select onChange={(event) => setLanguage(event.target.value)} value={language}>
              {(['multilingual', 'zh-CN', 'en'] as const).map((value) => <option key={value} value={value}>{t(`language.${value}`)}</option>)}
            </select>
          </label>
          <div className="kb-create-note">
            <NoteIcon size={17} />
            <span><strong>{t(`createNote.${kbType}.title`)}</strong><small>{t(`createNote.${kbType}.body`)}</small></span>
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to="/knowledge">{t('cancel')}</Link>
            <button className="primary-button" disabled={saving || !name.trim()} type="submit">{saving ? t('creating') : t('createAndContinue')} {!saving && <ArrowRight size={15} />}</button>
          </div>
        </form>
      </div>
    </section>
  )
}
