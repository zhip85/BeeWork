import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { currentLocale } from '../../i18n'
import type { TenantMembership } from '../../types'
import type { Assistant, AssistantCategory, AssistantModelOptions, AssistantSummary, OrganizationOption } from './types'
import { ASSISTANT_CATEGORIES } from './types'

/**
 * Creating an assistant asks only what is needed to start: name, what it is for, where it belongs and which model.
 * Everything else (prompt, capabilities, orchestration, channels) is set on the assistant page afterwards.
 */
export function AssistantCreateDialog({
  organizations, quota, tenant, onClose, onCreated,
}: {
  organizations: OrganizationOption[]
  quota: AssistantSummary['quota'] | null
  tenant: TenantMembership
  onClose: () => void
  onCreated: (assistant: Assistant) => void
}) {
  const { t } = useTranslation('assistants')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [organizationId, setOrganizationId] = useState(tenant.organization_id ?? '')
  const [category, setCategory] = useState<AssistantCategory>('customer_service')
  const [modelId, setModelId] = useState('')
  const [models, setModels] = useState<AssistantModelOptions | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenant.id)
      .then((options) => {
        if (!active) return
        setModels(options)
        setModelId((current) => current || options.default_model_id || '')
      })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [tenant.id])
  useEffect(() => { if (!organizationId && organizations[0]) setOrganizationId(organizations[0].id) }, [organizations, organizationId])

  const reached = quota?.limit != null && quota.used >= quota.limit
  const blocked = reached && quota?.policy === 'hard'
  const noModels = models !== null && models.models.length === 0
  const problem = !name.trim() ? t('wizard.errors.nameRequired') : !description.trim() ? t('wizard.errors.descriptionRequired') : !modelId ? t('wizard.errors.modelRequired') : ''

  async function create() {
    setSubmitting(true)
    setError('')
    try {
      const created = await apiRequest<Assistant>('/api/v1/assistants', {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(), description: description.trim(), organization_id: organizationId || undefined, category,
          visibility: 'own_org', visible_org_ids: [], draft: false,
          config: {
            dsl_version: 1, prompt: t('wizard.basics.defaultPrompt'), model_id: modelId || null, driven_by: null,
            default_language: currentLocale() === 'en' ? 'en' : 'zh-CN', greeting: t('wizard.experience.defaultGreeting'),
            suggested_questions: true, feedback_enabled: true, mounts: {}, oc: {},
          },
        }),
      }, tenant.id)
      onCreated(created)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('wizard.errors.saveFailed'))
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onClose()} role="presentation">
      <form aria-labelledby="create-title" aria-modal="true" className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); if (!problem && !blocked) void create() }} role="dialog">
        <button aria-label={t('wizard.close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <div className="section-kicker">{t('wizard.kickerNew')}</div>
        <h2 id="create-title">{t('wizard.titleNew')}</h2>
        <p className="usage-hint">{t('wizard.createHint')}</p>
        <label>{t('wizard.basics.name')} *<input autoFocus maxLength={50} onChange={(event) => setName(event.target.value)} placeholder={t('wizard.basics.namePlaceholder')} value={name} /></label>
        <label>{t('wizard.basics.description')} *<textarea maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder={t('wizard.basics.descriptionPlaceholder')} rows={3} value={description} /></label>
        {organizations.length > 0 && (
          <label>{t('wizard.basics.organization')}
            <select onChange={(event) => setOrganizationId(event.target.value)} value={organizationId}>
              {organizations.map((org) => <option key={org.id} value={org.id}>{'  '.repeat(org.depth)}{org.name}</option>)}
            </select>
          </label>
        )}
        <label>{t('wizard.basics.category')}
          <select onChange={(event) => setCategory(event.target.value as AssistantCategory)} value={category}>
            {ASSISTANT_CATEGORIES.map((key) => <option key={key} value={key}>{t(`category.${key}`)}</option>)}
          </select>
        </label>
        <label>{t('wizard.basics.model')} *
          <select disabled={noModels} onChange={(event) => setModelId(event.target.value)} value={modelId}>
            {!modelId && <option value="" />}
            {models?.models.map((model) => <option key={model.id} value={model.id}>{model.display_name}{model.is_default ? t('wizard.basics.modelDefault') : ''}</option>)}
          </select>
          {noModels && <small className="form-error">{t('wizard.basics.noModels')}</small>}
        </label>
        {blocked && quota && <p className="form-error" role="alert">{t('wizard.start.quotaReached', { used: quota.used, limit: quota.limit })}</p>}
        {reached && !blocked && <p className="assistants-notice" role="status">{t('wizard.start.quotaSoft')}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="plans-actions">
          <button className="text-button" disabled={submitting} onClick={onClose} type="button">{t('wizard.cancel')}</button>
          <button className="primary-button" disabled={submitting || blocked || Boolean(problem)} title={problem || undefined} type="submit">{submitting ? t('wizard.saving') : t('wizard.create')}</button>
        </div>
      </form>
    </div>
  )
}
