import { useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowLeft, ArrowRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import { FLOW_ICONS, FlowIcon, ICON_ORDER } from './FlowIcon'
import { DEFAULT_ICON, parseKind } from './types'
import type { FlowDetail, FlowIconName } from './types'

const NAME_MAX = 50
const DESCRIPTION_MAX = 200

/** Create page (3.3.2): name, description and icon; creating opens the editor on a new draft. */
export function FlowCreatePage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('flows')
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const kind = parseKind(params.get('type'))
  const kindName = t(`kind.${kind}`)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [icon, setIcon] = useState<FlowIconName>(DEFAULT_ICON[kind])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const listPath = kind === 'chatflow' ? '/flow-studio' : `/flow-studio?type=${kind}`

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const created = await apiRequest<FlowDetail>('/api/v1/agents', {
        method: 'POST',
        body: JSON.stringify({ kind, name: name.trim(), description: description.trim(), icon }),
      }, tenant.id)
      navigate(`/flow-studio/${created.id}`, { state: { notice: t('createdNotice') } })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setSaving(false)
    }
  }

  return (
    <section className="kb-create-page">
      <Link className="back-link" to={listPath}><ArrowLeft size={15} /> {t('backTo', { kind: kindName })} <span className="flows-draft-badge">{t('newDraft')}</span></Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <span className={`flows-icon large ${kind}`}><FlowIcon name={icon} size={24} /></span>
          <div>
            <span className="eyebrow">FLOW STUDIO</span>
            <h1>{t('createTitle', { kind: kindName })}</h1>
          </div>
        </header>
        <form className="kb-create-form" onSubmit={(event) => void submit(event)}>
          <div className="kb-form-heading">
            <span>01</span>
            <div><h2>{t('basicsTitle')}</h2><p>{t('basicsSubtitle', { kind: kindName })}</p></div>
          </div>
          <label className="kb-field">
            <span>{t('name')} <em>*</em></span>
            <input autoFocus maxLength={NAME_MAX} onChange={(event) => setName(event.target.value)} placeholder={t(`namePlaceholder.${kind}`)} required value={name} />
            <small>{t('nameHint')} {name.length}/{NAME_MAX}</small>
          </label>
          <label className="kb-field">
            <span>{t('description')}</span>
            <textarea maxLength={DESCRIPTION_MAX} onChange={(event) => setDescription(event.target.value)} placeholder={t('descriptionPlaceholder')} rows={3} value={description} />
            <small>{t('descriptionHint')} {description.length}/{DESCRIPTION_MAX}</small>
          </label>
          <fieldset className="kb-type-field">
            <legend>{t('icon')} <em>*</em></legend>
            <div className="flows-icon-grid" role="radiogroup">
              {ICON_ORDER.map((value) => {
                const Icon = FLOW_ICONS[value]
                return (
                  <label className={icon === value ? 'selected' : ''} key={value}>
                    <input checked={icon === value} name="icon" onChange={() => setIcon(value)} type="radio" />
                    <Icon size={18} /><span>{t(`iconName.${value}`)}</span>
                  </label>
                )
              })}
            </div>
            <small>{t('iconHint')}</small>
          </fieldset>
          <div className="kb-create-note"><ArrowRight size={17} /><span><strong>{t('nextStep')}</strong><small>{t(`nextHint.${kind}`)}</small></span></div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to={listPath}>{t('cancel')}</Link>
            <button className="primary-button" disabled={saving || !name.trim()} type="submit">{saving ? t('creating') : t('createAndConfigure')} {!saving && <ArrowRight size={15} />}</button>
          </div>
        </form>
      </div>
    </section>
  )
}
