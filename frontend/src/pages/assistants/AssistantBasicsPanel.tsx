import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import { ASSISTANT_CATEGORIES } from './types'
import type { Assistant, AssistantCategory, AssistantModelOptions, AssistantVisibility, OrganizationOption } from './types'

const MAX_PROMPT = 8000

type Form = {
  name: string
  description: string
  organizationId: string
  category: AssistantCategory
  prompt: string
  modelId: string
  role: string
  answerStyle: 'concise' | 'balanced' | 'detailed'
  fallbackModelId: string
  temperature: string
  maxOutput: string
  language: 'zh-CN' | 'en'
  visibility: AssistantVisibility
  visibleOrgIds: string[]
  greeting: string
  suggested: boolean
  feedback: boolean
}

const fromAssistant = (assistant: Assistant): Form => ({
  name: assistant.name,
  description: assistant.description,
  organizationId: assistant.organization_id,
  category: assistant.category,
  prompt: assistant.config.prompt,
  modelId: assistant.config.model_id ?? '',
  role: assistant.config.role ?? '',
  answerStyle: assistant.config.answer_style ?? 'balanced',
  fallbackModelId: assistant.config.fallback_model_id ?? '',
  temperature: String(assistant.config.temperature ?? 0.7),
  maxOutput: assistant.config.max_output_tokens ? String(assistant.config.max_output_tokens) : '',
  language: assistant.config.default_language,
  visibility: assistant.visibility,
  visibleOrgIds: assistant.visible_org_ids,
  greeting: assistant.config.greeting,
  suggested: assistant.config.suggested_questions,
  feedback: assistant.config.feedback_enabled,
})

/** The “基础” section of the assistant page: identity, model, language, visibility and experience, edited in place. */
export function AssistantBasicsPanel({
  assistant, organizations, tenant, canEdit, onSaved,
}: {
  assistant: Assistant
  organizations: OrganizationOption[]
  tenant: TenantMembership
  canEdit: boolean
  onSaved: () => void
}) {
  const { t } = useTranslation('assistants')
  const { t: td } = useTranslation('detail')
  const [form, setForm] = useState<Form>(() => fromAssistant(assistant))
  const [models, setModels] = useState<AssistantModelOptions | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const baseline = JSON.stringify(fromAssistant(assistant))
  const dirty = JSON.stringify(form) !== baseline

  // A newer saved version (after saving, or a reload) replaces the form; unsaved typing is never overwritten by it.
  useEffect(() => { setForm(fromAssistant(assistant)) }, [assistant.row_version, assistant.id]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let active = true
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenant.id).then((options) => active && setModels(options)).catch(() => undefined)
    return () => { active = false }
  }, [tenant.id])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const update = <K extends keyof Form>(key: K, value: Form[K]) => { setForm((current) => ({ ...current, [key]: value })); setNotice('') }
  const problem = !form.name.trim() ? t('wizard.errors.nameRequired')
    : !form.description.trim() ? t('wizard.errors.descriptionRequired')
    : !form.prompt.trim() ? t('wizard.errors.promptRequired')
    : !form.modelId ? t('wizard.errors.modelRequired')
    : !(Number(form.temperature) >= 0 && Number(form.temperature) <= 2) ? t('wizard.errors.temperatureRange')
    : form.maxOutput && !(Number(form.maxOutput) >= 16 && Number(form.maxOutput) <= 8192) ? t('wizard.errors.maxOutputRange') : ''
  const noModels = models !== null && models.models.length === 0
  const lockedOrganization = tenant.role !== 'tenant_admin'

  async function save() {
    setSaving(true)
    setError('')
    try {
      await apiRequest<Assistant>(`/api/v1/assistants/${assistant.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: form.name.trim(),
          description: form.description.trim(),
          organization_id: form.organizationId || undefined,
          category: form.category,
          visibility: form.visibility,
          visible_org_ids: form.visibility === 'specified' ? form.visibleOrgIds : [],
          draft: false,
          row_version: assistant.row_version,
          config: {
            ...assistant.config,
            prompt: form.prompt,
            model_id: form.modelId || null,
            role: form.role.trim(),
            answer_style: form.answerStyle,
            fallback_model_id: form.fallbackModelId || null,
            temperature: Number(form.temperature),
            max_output_tokens: form.maxOutput ? Number(form.maxOutput) : null,
            default_language: form.language,
            greeting: form.greeting,
            suggested_questions: form.suggested,
            feedback_enabled: form.feedback,
          },
        }),
      }, tenant.id)
      setNotice(t('toast.saved'))
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('wizard.errors.saveFailed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="detail-card wizard-form assistant-basics" onSubmit={(event) => { event.preventDefault(); if (!problem) void save() }}>
      <header className="detail-card-head">
        <div><h2>{td('basic.title')}</h2><p className="usage-hint">{td('basic.hint')}</p></div>
        {canEdit && (
          <div className="plans-actions">
            {dirty && <button className="text-button" disabled={saving} onClick={() => setForm(fromAssistant(assistant))} type="button">{td('basic.reset')}</button>}
            <button className="primary-button" disabled={!dirty || saving || Boolean(problem)} type="submit">{saving ? t('wizard.saving') : t('wizard.save')}</button>
          </div>
        )}
      </header>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {dirty && problem && <p className="wizard-muted">{problem}</p>}
      <fieldset className="basics-fieldset" disabled={!canEdit}>
        <label>{t('wizard.basics.name')}<input maxLength={50} onChange={(event) => update('name', event.target.value)} value={form.name} /></label>
        <label>{t('wizard.basics.description')}<textarea maxLength={500} onChange={(event) => update('description', event.target.value)} rows={3} value={form.description} /></label>
        {organizations.length > 0 && (
          <label>{t('wizard.basics.organization')}
            <select disabled={lockedOrganization} onChange={(event) => update('organizationId', event.target.value)} value={form.organizationId}>
              {organizations.map((org) => <option key={org.id} value={org.id}>{'  '.repeat(org.depth)}{org.name}</option>)}
            </select>
            <small>{t('wizard.basics.organizationLocked')}</small>
          </label>
        )}
        <label>{t('wizard.basics.category')}
          <select onChange={(event) => update('category', event.target.value as AssistantCategory)} value={form.category}>
            {ASSISTANT_CATEGORIES.map((key) => <option key={key} value={key}>{t(`category.${key}`)}</option>)}
          </select>
        </label>
        <label>{t('wizard.basics.role')}<input maxLength={200} onChange={(event) => update('role', event.target.value)} placeholder={t('wizard.basics.rolePlaceholder')} value={form.role} /></label>
        <label>{t('wizard.basics.answerStyle')}
          <select onChange={(event) => update('answerStyle', event.target.value as Form['answerStyle'])} value={form.answerStyle}>
            {(['concise', 'balanced', 'detailed'] as const).map((key) => <option key={key} value={key}>{t(`wizard.basics.styles.${key}`)}</option>)}
          </select>
        </label>
        <label>{t('wizard.basics.prompt')}
          <textarea maxLength={MAX_PROMPT} onChange={(event) => update('prompt', event.target.value)} rows={12} value={form.prompt} />
          <small>{t('wizard.basics.chars', { count: form.prompt.length, max: MAX_PROMPT })}</small>
        </label>
        <label>{t('wizard.basics.model')}
          <select disabled={noModels} onChange={(event) => update('modelId', event.target.value)} value={form.modelId}>
            {!form.modelId && <option value="" />}
            {models?.models.map((model) => <option key={model.id} value={model.id}>{model.display_name}{model.is_default ? t('wizard.basics.modelDefault') : ''}</option>)}
          </select>
          {noModels && <small className="form-error">{t('wizard.basics.noModels')}</small>}
        </label>
        <label>{t('wizard.basics.fallbackModel')}
          <select disabled={noModels} onChange={(event) => update('fallbackModelId', event.target.value)} value={form.fallbackModelId}>
            <option value="">{t('wizard.basics.fallbackNone')}</option>
            {models?.models.filter((model) => model.id !== form.modelId).map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
          </select>
          <small>{t('wizard.basics.fallbackHint')}</small>
        </label>
        <div className="basics-row">
          <label>{t('wizard.basics.temperature')}<input max={2} min={0} onChange={(event) => update('temperature', event.target.value)} step={0.1} type="number" value={form.temperature} /><small>{t('wizard.basics.temperatureHint')}</small></label>
          <label>{t('wizard.basics.maxOutput')}<input max={8192} min={16} onChange={(event) => update('maxOutput', event.target.value)} placeholder={t('wizard.basics.maxOutputDefault')} type="number" value={form.maxOutput} /></label>
        </div>
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
              {organizations.map((org) => (
                <label className="wizard-radio" key={org.id}>
                  <input checked={form.visibleOrgIds.includes(org.id)} onChange={(event) => update('visibleOrgIds', event.target.checked ? [...form.visibleOrgIds, org.id] : form.visibleOrgIds.filter((id) => id !== org.id))} type="checkbox" />
                  {org.name}
                </label>
              ))}
            </div>
          )}
        </fieldset>
        <h3>{td('basic.experience')}</h3>
        <label>{t('wizard.experience.greeting')}<textarea maxLength={500} onChange={(event) => update('greeting', event.target.value)} rows={3} value={form.greeting} /></label>
        <label className="wizard-toggle"><input checked={form.suggested} onChange={(event) => update('suggested', event.target.checked)} type="checkbox" /><span><strong>{t('wizard.experience.suggested')}</strong><small>{t('wizard.experience.suggestedHint')}</small></span></label>
        <label className="wizard-toggle"><input checked={form.feedback} onChange={(event) => update('feedback', event.target.checked)} type="checkbox" /><span><strong>{t('wizard.experience.feedback')}</strong><small>{t('wizard.experience.feedbackHint')}</small></span></label>
      </fieldset>
    </form>
  )
}
