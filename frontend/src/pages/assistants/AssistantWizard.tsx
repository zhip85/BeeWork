import { useEffect, useState } from 'react'
import { Check, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { currentLocale } from '../../i18n'
import type { TenantMembership } from '../../types'
import { ASSISTANT_CATEGORIES } from './types'
import type {
  Assistant,
  AssistantCategory,
  AssistantModelOptions,
  AssistantSummary,
  AssistantVisibility,
  OrganizationOption,
} from './types'

const STEP_KEYS = ['start', 'basics', 'capabilities', 'experience'] as const
const MOUNT_KEYS = ['kb', 'agent', 'mcp', 'skill', 'chatflow', 'flow', 'api'] as const
const MAX_PROMPT = 8000

type Form = {
  name: string
  description: string
  organizationId: string
  category: AssistantCategory
  prompt: string
  modelId: string
  language: 'zh-CN' | 'en'
  visibility: AssistantVisibility
  visibleOrgIds: string[]
  greeting: string
  suggested: boolean
  feedback: boolean
}

export function AssistantWizard({
  assistant,
  organizations,
  quota,
  tenant,
  onClose,
  onSaved,
}: {
  assistant: Assistant | null
  organizations: OrganizationOption[]
  quota: AssistantSummary['quota'] | null
  tenant: TenantMembership
  onClose: () => void
  onSaved: (messageKey: string, close: boolean) => void
}) {
  const { t } = useTranslation('assistants')
  const editing = assistant !== null
  const [step, setStep] = useState(editing ? 1 : 0)
  const [saved, setSaved] = useState<{ id: string; rowVersion: number } | null>(
    assistant ? { id: assistant.id, rowVersion: assistant.row_version } : null,
  )
  const [dirty, setDirty] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [confirmClose, setConfirmClose] = useState(false)
  const [models, setModels] = useState<AssistantModelOptions | null>(null)
  const [form, setForm] = useState<Form>(() => ({
    name: assistant?.name ?? '',
    description: assistant?.description ?? '',
    organizationId: assistant?.organization_id ?? tenant.organization_id ?? '',
    category: assistant?.category ?? 'customer_service',
    prompt: assistant?.config.prompt ?? t('wizard.basics.defaultPrompt'),
    modelId: assistant?.config.model_id ?? '',
    language: assistant?.config.default_language ?? (currentLocale() === 'en' ? 'en' : 'zh-CN'),
    visibility: assistant?.visibility ?? 'own_org',
    visibleOrgIds: assistant?.visible_org_ids ?? [],
    greeting: assistant?.config.greeting ?? t('wizard.experience.defaultGreeting'),
    suggested: assistant?.config.suggested_questions ?? true,
    feedback: assistant?.config.feedback_enabled ?? true,
  }))

  useEffect(() => {
    let active = true
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenant.id)
      .then((options) => {
        if (!active) return
        setModels(options)
        setForm((current) => (current.modelId || !options.default_model_id ? current : { ...current, modelId: options.default_model_id }))
      })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [tenant.id])

  const update = <K extends keyof Form>(key: K, value: Form[K]) => {
    setForm((current) => ({ ...current, [key]: value }))
    setDirty(true)
  }

  const quotaBlocked = !editing && !saved && quota?.limit != null && quota.used >= quota.limit && quota.policy === 'hard'
  const quotaSoft = !editing && !saved && quota?.limit != null && quota.used >= quota.limit && quota.policy !== 'hard'
  const noModels = models !== null && models.models.length === 0

  function stepError(index: number): string {
    if (index !== 1) return ''
    if (!form.name.trim()) return t('wizard.errors.nameRequired')
    if (!form.description.trim()) return t('wizard.errors.descriptionRequired')
    if (!form.prompt.trim()) return t('wizard.errors.promptRequired')
    if (!form.modelId) return t('wizard.errors.modelRequired')
    return ''
  }

  const blockingError = stepError(step)

  function payload(draft: boolean) {
    return {
      name: form.name.trim() || null,
      description: form.description.trim(),
      organization_id: form.organizationId || undefined,
      category: form.category,
      visibility: form.visibility,
      visible_org_ids: form.visibility === 'specified' ? form.visibleOrgIds : [],
      draft,
      row_version: saved?.rowVersion,
      config: {
        dsl_version: 1,
        prompt: form.prompt,
        model_id: form.modelId || null,
        driven_by: null,
        default_language: form.language,
        greeting: form.greeting,
        suggested_questions: form.suggested,
        feedback_enabled: form.feedback,
        mounts: assistant?.config.mounts ?? {},
        oc: assistant?.config.oc ?? {},
      },
    }
  }

  async function save(draft: boolean) {
    setSubmitting(true)
    setError('')
    try {
      const result = saved
        ? await apiRequest<Assistant>(`/api/v1/assistants/${saved.id}`, { method: 'PUT', body: JSON.stringify(payload(draft)) }, tenant.id)
        : await apiRequest<Assistant>('/api/v1/assistants', { method: 'POST', body: JSON.stringify(payload(draft)) }, tenant.id)
      setSaved({ id: result.id, rowVersion: result.row_version })
      setDirty(false)
      setForm((current) => ({ ...current, name: result.name }))
      onSaved(draft ? 'toast.draftSaved' : editing ? 'toast.saved' : 'toast.created', !draft)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('wizard.errors.saveFailed'))
    } finally {
      setSubmitting(false)
    }
  }

  function requestClose() {
    if (dirty) setConfirmClose(true)
    else onClose()
  }

  const lastStep = step === STEP_KEYS.length - 1
  const orgChoices = organizations

  return (
    <div className="modal-backdrop wizard-backdrop" role="presentation">
      <div aria-labelledby="wizard-title" aria-modal="true" className="assistant-wizard" role="dialog">
        <header>
          <div>
            <p className="section-kicker">{t(editing ? 'wizard.kickerEdit' : 'wizard.kickerNew')}</p>
            <h2 id="wizard-title">{t(editing ? 'wizard.titleEdit' : 'wizard.titleNew')}</h2>
          </div>
          <button aria-label={t('wizard.close')} className="modal-close wizard-close" onClick={requestClose} type="button"><X size={18} /></button>
        </header>
        <ol className="wizard-steps" aria-label="steps">
          {STEP_KEYS.map((key, index) => (
            <li aria-current={index === step ? 'step' : undefined} className={index === step ? 'current' : index < step ? 'done' : ''} key={key}>
              <span>{index < step ? <Check size={12} /> : index + 1}</span>{t(`wizard.steps.${key}`)}
            </li>
          ))}
        </ol>

        <div className="wizard-body">
          {step === 0 && (
            <div>
              <h3>{t('wizard.start.title')}</h3>
              <p>{t('wizard.start.hint')}</p>
              <div className="wizard-choices">
                <div className="wizard-choice selected" aria-checked="true" role="radio" tabIndex={0}><strong>{t('wizard.start.blank')}</strong><small>{t('wizard.start.blankHint')}</small><Check size={14} /></div>
                <div className="wizard-choice disabled" aria-disabled="true"><strong>{t('wizard.start.support')}</strong><small>{t('wizard.start.supportHint')}</small><em>{t('wizard.start.soon')}</em></div>
                <div className="wizard-choice disabled" aria-disabled="true"><strong>{t('wizard.start.sales')}</strong><small>{t('wizard.start.salesHint')}</small><em>{t('wizard.start.soon')}</em></div>
              </div>
              <p className="wizard-muted">{t('wizard.start.browse')} · {t('wizard.start.soon')}</p>
              {quotaBlocked && quota && <p className="form-error" role="alert">{t('wizard.start.quotaReached', { used: quota.used, limit: quota.limit })}</p>}
              {quotaSoft && <p className="assistants-notice" role="status">{t('wizard.start.quotaSoft')}</p>}
            </div>
          )}

          {step === 1 && (
            <div className="wizard-form">
              <label>{t('wizard.basics.name')}<input autoFocus maxLength={50} onChange={(event) => update('name', event.target.value)} placeholder={t('wizard.basics.namePlaceholder')} value={form.name} /></label>
              <label>{t('wizard.basics.description')}<textarea maxLength={500} onChange={(event) => update('description', event.target.value)} placeholder={t('wizard.basics.descriptionPlaceholder')} rows={3} value={form.description} /></label>
              {orgChoices.length > 0 && (
                <label>{t('wizard.basics.organization')}
                  <select disabled={editing && tenant.role !== 'tenant_admin'} onChange={(event) => update('organizationId', event.target.value)} value={form.organizationId}>
                    {orgChoices.map((org) => <option key={org.id} value={org.id}>{'  '.repeat(org.depth)}{org.name}</option>)}
                  </select>
                  {editing && <small>{t('wizard.basics.organizationLocked')}</small>}
                </label>
              )}
              <label>{t('wizard.basics.category')}
                <select onChange={(event) => update('category', event.target.value as AssistantCategory)} value={form.category}>
                  {ASSISTANT_CATEGORIES.map((key) => <option key={key} value={key}>{t(`category.${key}`)}</option>)}
                </select>
              </label>
              <label>{t('wizard.basics.prompt')}
                <textarea maxLength={MAX_PROMPT} onChange={(event) => update('prompt', event.target.value)} rows={10} value={form.prompt} />
                <small>{t('wizard.basics.chars', { count: form.prompt.length, max: MAX_PROMPT })}
                  <button className="text-button" disabled title={t('wizard.basics.aiSuggestSoon')} type="button">{t('wizard.basics.aiSuggest')}</button>
                </small>
              </label>
              <label>{t('wizard.basics.model')}
                <select disabled={noModels} onChange={(event) => update('modelId', event.target.value)} value={form.modelId}>
                  {!form.modelId && <option value="" />}
                  {models?.models.map((model) => <option key={model.id} value={model.id}>{model.display_name}{model.is_default ? t('wizard.basics.modelDefault') : ''}</option>)}
                </select>
                {noModels && <small className="form-error">{t('wizard.basics.noModels')}</small>}
              </label>
              <label>{t('wizard.basics.driver')}<select disabled value="none"><option value="none">{t('wizard.basics.driverNone')}</option></select><small>{t('wizard.basics.driverSoon')}</small></label>
              <label>{t('wizard.basics.language')}
                <select onChange={(event) => update('language', event.target.value as 'zh-CN' | 'en')} value={form.language}>
                  {(['zh-CN', 'en'] as const).map((key) => <option key={key} value={key}>{t(`wizard.language.${key}`)}</option>)}
                </select>
              </label>
              <fieldset>
                <legend>{t('wizard.basics.visibility')}</legend>
                {(['own_org', 'specified', 'all'] as const).map((key) => (
                  <label className="wizard-radio" key={key}>
                    <input checked={form.visibility === key} name="visibility" onChange={() => update('visibility', key)} type="radio" />
                    {t(key === 'own_org' ? 'wizard.basics.visibilityOwn' : key === 'specified' ? 'wizard.basics.visibilitySpecified' : 'wizard.basics.visibilityAll')}
                  </label>
                ))}
                {form.visibility === 'specified' && (
                  <div className="wizard-org-list" aria-label={t('wizard.basics.visibleOrgs')}>
                    {orgChoices.map((org) => (
                      <label className="wizard-radio" key={org.id}>
                        <input checked={form.visibleOrgIds.includes(org.id)} onChange={(event) => update('visibleOrgIds', event.target.checked ? [...form.visibleOrgIds, org.id] : form.visibleOrgIds.filter((id) => id !== org.id))} type="checkbox" />
                        {org.name}
                      </label>
                    ))}
                  </div>
                )}
              </fieldset>
            </div>
          )}

          {step === 2 && (
            <div>
              <h3>{t('wizard.capabilities.title')}</h3>
              <p>{t('wizard.capabilities.hint')}</p>
              <ul className="wizard-capabilities">
                {MOUNT_KEYS.map((key) => <li key={key}><span>{t(`wizard.capabilities.${key}`)}</span><em>{t('wizard.capabilities.soon')}</em></li>)}
              </ul>
            </div>
          )}

          {step === 3 && (
            <div className="wizard-form">
              <label>{t('wizard.experience.greeting')}<textarea maxLength={500} onChange={(event) => update('greeting', event.target.value)} rows={3} value={form.greeting} /></label>
              <label className="wizard-toggle"><input checked={form.suggested} onChange={(event) => update('suggested', event.target.checked)} type="checkbox" /><span><strong>{t('wizard.experience.suggested')}</strong><small>{t('wizard.experience.suggestedHint')}</small></span></label>
              <label className="wizard-toggle"><input checked={form.feedback} onChange={(event) => update('feedback', event.target.checked)} type="checkbox" /><span><strong>{t('wizard.experience.feedback')}</strong><small>{t('wizard.experience.feedbackHint')}</small></span></label>
              <p className="wizard-ready">{t('wizard.experience.ready', { tenant: tenant.name })}</p>
            </div>
          )}
        </div>

        {error && <p className="form-error wizard-error" role="alert">{error}</p>}
        {step === 1 && blockingError && <p className="wizard-muted wizard-hint">{blockingError}</p>}

        <footer>
          {step === 0 || (editing && step === 1)
            ? <button className="secondary-button" disabled={submitting} onClick={requestClose} type="button">{t('wizard.cancel')}</button>
            : <button className="secondary-button" disabled={submitting} onClick={() => setStep(step - 1)} type="button">{t('wizard.back')}</button>}
          <span className="wizard-footer-spacer" />
          <button className="secondary-button" disabled={submitting || quotaBlocked} onClick={() => void save(true)} type="button">{submitting ? t('wizard.saving') : t('wizard.saveDraft')}</button>
          {lastStep
            ? <button className="primary-button" disabled={submitting || quotaBlocked || Boolean(stepError(1))} onClick={() => void save(false)} type="button">{submitting ? t('wizard.saving') : t(editing ? 'wizard.save' : 'wizard.create')}</button>
            : <button className="primary-button" disabled={submitting || quotaBlocked || Boolean(blockingError)} onClick={() => setStep(step + 1)} type="button">{t('wizard.next')}</button>}
        </footer>
      </div>

      {confirmClose && (
        <ConfirmDialog
          body={t('confirm.discardBody')}
          cancelLabel={t('confirm.keepEditing')}
          confirmLabel={t('confirm.discard')}
          danger
          onCancel={() => setConfirmClose(false)}
          onConfirm={onClose}
          title={t('confirm.discardTitle')}
        />
      )}
    </div>
  )
}
