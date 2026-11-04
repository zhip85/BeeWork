import { useState } from 'react'
import type { FormEvent } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantEnvironmentName } from '../../types'

export type Channel = {
  id: string
  name: string
  assistant_id: string
  assistant_name: string | null
  environment: TenantEnvironmentName
  allowed_origins: string[]
  experience: { primary_color: string; assistant_name: string; welcome: string }
  effective_experience: Record<string, string>
  status: 'draft' | 'active' | 'disabled'
  site_key: string
  published_version: string
  has_unpublished_changes: boolean
  pending_approval_id: string | null
  row_version: number
  embed_code: string | null
}

export type AssistantOption = { id: string; name: string }

/** Create or edit a website channel (the minimal wizard of T-040: one form for basics, domains and experience). */
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
  const [name, setName] = useState(channel?.name ?? '')
  const [assistantId, setAssistantId] = useState(channel?.assistant_id ?? presetAssistantId ?? assistants[0]?.id ?? '')
  const [environment, setEnvironment] = useState<TenantEnvironmentName>(channel?.environment ?? 'production')
  const [origins, setOrigins] = useState((channel?.allowed_origins ?? []).join('\n'))
  const [color, setColor] = useState(channel?.experience.primary_color ?? '')
  const [assistantName, setAssistantName] = useState(channel?.experience.assistant_name ?? '')
  const [welcome, setWelcome] = useState(channel?.experience.welcome ?? '')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      await apiRequest(channel ? `/api/v1/channels/${channel.id}` : '/api/v1/channels', {
        method: channel ? 'PUT' : 'POST',
        body: JSON.stringify({
          name, assistant_id: assistantId, environment,
          allowed_origins: origins.split('\n').map((line) => line.trim()).filter(Boolean),
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
      <form aria-labelledby="channel-title" aria-modal="true" className="create-modal channel-dialog" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <h2 id="channel-title">{channel ? t('dialog.edit') : t('dialog.create')}</h2>
        <label>{t('fields.name')}<input autoFocus maxLength={50} onChange={(event) => setName(event.target.value)} placeholder={t('fields.namePlaceholder')} required value={name} /></label>
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
        <label>{t('fields.origins')}
          <textarea onChange={(event) => setOrigins(event.target.value)} placeholder="https://www.example.com" rows={3} value={origins} />
          <small>{t('fields.originsHint')}</small>
        </label>
        <div className="channel-experience">
          <label>{t('fields.color')}
            <span className="color-row"><input onChange={(event) => setColor(event.target.value)} type="color" value={color || '#6558d9'} /><input maxLength={7} onChange={(event) => setColor(event.target.value)} placeholder="#6558d9" value={color} /></span>
          </label>
          <label>{t('fields.assistantName')}<input maxLength={50} onChange={(event) => setAssistantName(event.target.value)} placeholder={t('fields.inherit')} value={assistantName} /></label>
        </div>
        <label>{t('fields.welcome')}<textarea maxLength={500} onChange={(event) => setWelcome(event.target.value)} placeholder={t('fields.inherit')} rows={2} value={welcome} /></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={saving || !name.trim() || !assistantId} type="submit">{saving ? t('saving') : t('save')}</button>
        </div>
      </form>
    </div>
  )
}
