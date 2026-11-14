import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Check, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantEnvironmentName } from '../../types'

export type ChannelStatus = 'draft' | 'active' | 'error' | 'disabled'
export type ConnectionParam = { key: string; label: string; kind: 'text' | 'secret' | 'origins' | 'generated'; required: boolean; placeholder?: string }
export type ChannelType = {
  code: string
  name: string
  version: string
  enabled: boolean
  available: boolean
  unavailable_reason: 'disabled' | 'plan' | null
  connection_params: ConnectionParam[]
  capabilities: string[]
  default_experience: Record<string, unknown>
}

export type Channel = {
  id: string
  type: string
  type_name: string
  type_enabled: boolean
  name: string
  assistant_id: string
  assistant_name: string | null
  environment: TenantEnvironmentName
  allowed_origins: string[]
  config: Record<string, string>
  secret_masks: Record<string, string>
  experience: { primary_color: string; assistant_name: string; welcome: string }
  effective_experience: Record<string, string>
  status: ChannelStatus
  health_error: string | null
  site_key: string
  published_version: string
  has_unpublished_changes: boolean
  pending_approval_id: string | null
  row_version: number
  embed_code: string | null
  integration: { kind: string; code: string } | null
}

export type AssistantOption = { id: string; name: string }

/** The channel wizard (T-041, CHN-010/011): step 1 basics and connection, step 2 components and quick experience, with a live preview. */
export function ChannelDialog({
  channel,
  assistants,
  presetAssistantId,
  environments,
  tenantId,
  onClose,
  onSaved,
}: {
  channel: Channel | null
  assistants: AssistantOption[]
  presetAssistantId?: string
  environments: TenantEnvironmentName[]
  tenantId: string
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation('channels')
  const [types, setTypes] = useState<ChannelType[]>([])
  const [step, setStep] = useState<1 | 2>(1)
  const [type, setType] = useState(channel?.type ?? 'website')
  const [name, setName] = useState(channel?.name ?? '')
  const [assistantId, setAssistantId] = useState(channel?.assistant_id ?? presetAssistantId ?? assistants[0]?.id ?? '')
  const [environment, setEnvironment] = useState<TenantEnvironmentName>(channel?.environment ?? 'production')
  const [config, setConfig] = useState<Record<string, string>>(channel?.config ?? {})
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [origins, setOrigins] = useState((channel?.allowed_origins ?? []).join('\n'))
  const [color, setColor] = useState(channel?.experience.primary_color ?? '')
  const [assistantName, setAssistantName] = useState(channel?.experience.assistant_name ?? '')
  const [welcome, setWelcome] = useState(channel?.experience.welcome ?? '')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const locked = channel !== null && channel.status !== 'draft' ? true : (channel?.published_version ?? '—') !== '—'

  useEffect(() => {
    let active = true
    apiRequest<{ items: ChannelType[] }>('/api/v1/channel-types', {}, tenantId)
      .then((result) => active && setTypes(result.items))
      .catch(() => active && setTypes([]))
    return () => { active = false }
  }, [tenantId])

  const current = types.find((item) => item.code === type)
  const params = current?.connection_params ?? []
  const defaults = current?.default_experience ?? {}
  const shownColor = color || (typeof defaults.primary_color === 'string' ? defaults.primary_color : '#6558d9')
  const shownWelcome = welcome || (typeof defaults.welcome === 'string' ? defaults.welcome : t('preview.defaultWelcome'))
  const stored = useMemo(() => channel?.secret_masks ?? {}, [channel])

  function switchType(next: string) {
    if (next === type) return
    setType(next)
    setConfig({})
    setSecrets({})
    setOrigins('')
    setColor('')
    setWelcome('')  // CHN-010: switching type resets the type's own settings
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (step === 1) { setStep(2); return }
    setSaving(true)
    setError('')
    try {
      await apiRequest(channel ? `/api/v1/channels/${channel.id}` : '/api/v1/channels', {
        method: channel ? 'PUT' : 'POST',
        body: JSON.stringify({
          type, name, assistant_id: assistantId, environment, config, secrets,
          allowed_origins: params.some((p) => p.kind === 'origins') ? origins.split('\n').map((line) => line.trim()).filter(Boolean) : [],
          experience: { primary_color: color, assistant_name: assistantName, welcome },
          row_version: channel?.row_version,
        }),
      }, tenantId)
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('saveFailed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <form aria-labelledby="channel-title" aria-modal="true" className="create-modal channel-dialog channel-wizard" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <h2 id="channel-title">{channel ? t('dialog.edit') : t('dialog.create')}</h2>
        <ol className="channel-steps" aria-label={t('wizard.steps')}>
          {([1, 2] as const).map((value) => (
            <li className={step === value ? 'current' : ''} key={value}>
              <button disabled={value === 2 && !name.trim()} onClick={() => setStep(value)} type="button">
                {step > value ? <Check size={12} /> : value} {t(`wizard.step${value}`)}
              </button>
            </li>
          ))}
        </ol>
        <div className="channel-wizard-body">
          <div className="channel-wizard-form">
            {step === 1 && (
              <>
                <label>{t('fields.name')}<input autoFocus maxLength={50} onChange={(event) => setName(event.target.value)} placeholder={t('fields.namePlaceholder')} required value={name} /></label>
                <label>{t('fields.type')}
                  <select disabled={locked} onChange={(event) => switchType(event.target.value)} value={type}>
                    {(types.length ? types : [{ code: type, name: type, available: true } as ChannelType]).map((item) => (
                      <option disabled={!item.available && item.code !== channel?.type} key={item.code} value={item.code}>
                        {item.name}{!item.available && item.unavailable_reason ? `（${t(`unavailable.${item.unavailable_reason}`)}）` : ''}
                      </option>
                    ))}
                  </select>
                  <small>{locked ? t('fields.typeLocked') : current ? current.capabilities.join(' · ') : ''}</small>
                </label>
                <label>{t('fields.assistant')}
                  <select onChange={(event) => setAssistantId(event.target.value)} required value={assistantId}>
                    {assistants.map((assistant) => <option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}
                  </select>
                  <small>{t('fields.assistantHint')}</small>
                </label>
                <label>{t('fields.environment')}
                  <select onChange={(event) => setEnvironment(event.target.value as TenantEnvironmentName)} value={environment}>
                    {environments.map((item) => <option key={item} value={item}>{t(`environments.${item}`)}</option>)}
                  </select>
                  <small>{t('fields.environmentHint')}</small>
                </label>
                <fieldset className="channel-connection">
                  <legend>{t('fields.connection')}</legend>
                  {params.map((param) => {
                    if (param.kind === 'origins') {
                      return (
                        <label key={param.key}>{t('fields.origins')}
                          <textarea onChange={(event) => setOrigins(event.target.value)} placeholder="https://www.example.com" rows={3} value={origins} />
                          <small>{t('fields.originsHint')}</small>
                        </label>
                      )
                    }
                    if (param.kind === 'generated') {
                      return <p className="wizard-muted" key={param.key}><strong>{param.label}</strong> {t('fields.generated')}</p>
                    }
                    if (param.kind === 'secret') {
                      return (
                        <label key={param.key}>{param.label}{param.required && ' *'}
                          <input autoComplete="off" onChange={(event) => setSecrets({ ...secrets, [param.key]: event.target.value })} placeholder={stored[param.key] ?? ''} type="password" value={secrets[param.key] ?? ''} />
                          <small>{stored[param.key] ? t('fields.secretKeep') : t('fields.secretHint')}</small>
                        </label>
                      )
                    }
                    return (
                      <label key={param.key}>{param.label}{param.required && ' *'}
                        <input maxLength={200} onChange={(event) => setConfig({ ...config, [param.key]: event.target.value })} placeholder={param.placeholder ?? ''} value={config[param.key] ?? ''} />
                      </label>
                    )
                  })}
                </fieldset>
              </>
            )}
            {step === 2 && (
              <>
                <section className="channel-components">
                  <h3>{t('wizard.components')}</h3>
                  <p className="wizard-muted">{t('wizard.componentsPending')}</p>
                </section>
                <div className="channel-experience">
                  <label>{t('fields.color')}
                    <span className="color-row"><input onChange={(event) => setColor(event.target.value)} type="color" value={shownColor} /><input maxLength={7} onChange={(event) => setColor(event.target.value)} placeholder={shownColor} value={color} /></span>
                  </label>
                  <label>{t('fields.assistantName')}<input maxLength={50} onChange={(event) => setAssistantName(event.target.value)} placeholder={t('fields.inherit')} value={assistantName} /></label>
                </div>
                <label>{t('fields.welcome')}<textarea maxLength={500} onChange={(event) => setWelcome(event.target.value)} placeholder={t('fields.inherit')} rows={2} value={welcome} /></label>
              </>
            )}
          </div>
          <aside aria-label={t('preview.title', { type: current?.name ?? type })} className="channel-preview">
            <strong>{t('preview.title', { type: current?.name ?? type })}</strong>
            {type === 'open-api' ? (
              <pre>{'{\n  "message": "…",\n  "widgets": [],\n  "quick_replies": [],\n  "stream": true\n}'}</pre>
            ) : (
              <div className="channel-phone">
                <header style={{ background: shownColor }}>{name || t('preview.unnamed')}</header>
                <p className="bubble bot">{shownWelcome}</p>
                <p className="bubble user">{t('preview.user')}</p>
                <p className="bubble bot">{t('preview.plain')}</p>
              </div>
            )}
          </aside>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('cancel')}</button>
          {step === 2 && <button className="secondary-button" onClick={() => setStep(1)} type="button">{t('wizard.back')}</button>}
          {step === 1
            ? <button className="primary-button" disabled={!name.trim() || !assistantId} type="submit">{t('wizard.next')}</button>
            : <button className="primary-button" disabled={saving || !name.trim() || !assistantId} type="submit">{saving ? t('saving') : t('save')}</button>}
        </div>
      </form>
    </div>
  )
}
