import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { StagePanelProps } from './stagePanels'

type ComponentsConfig = { enabled: boolean; max_per_turn: number; on_invalid: 'text' | 'retry' | 'drop'; text_template: string }
type Injection = {
  enabled: boolean
  channels: { channel_id: string; name: string; type: string; type_name: string; published_version: string; published: boolean; components: { trigger_tag: string; name: string; mode: string; version: string }[]; prompt: string | null }[]
}

const DEFAULTS: ComponentsConfig = { enabled: true, max_per_turn: 2, on_invalid: 'text', text_template: '{{title}}\n{{fields}}' }

/** OC · components (OC-070 – OC-074): the switch and rules, and a read-only preview of what each bound channel injects from its published snapshot. */
export function ComponentsStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('orchestration')
  const config: ComponentsConfig = { ...DEFAULTS, ...((oc.components ?? {}) as Partial<ComponentsConfig>) }
  const [preview, setPreview] = useState<Injection | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState('')
  const set = (next: Partial<ComponentsConfig>) => onChange({ ...oc, components: { ...config, ...next } })
  const off = readOnly || !config.enabled

  useEffect(() => {
    let active = true
    apiRequest<Injection>(`/api/v1/assistants/${assistantId}/oc/components/preview`, {}, tenant.id)
      .then((value) => { if (active) { setPreview(value); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [assistantId, tenant.id])

  return (
    <div className="oc-components-panel">
      <p className="usage-hint">{t('components.hint')}</p>
      <label className="governance-inherit">
        <input checked={config.enabled} disabled={readOnly} onChange={(event) => set({ enabled: event.target.checked })} type="checkbox" />
        <span><strong>{t('components.enable')}</strong><small>{t('components.enableHint')}</small></span>
      </label>
      <label className="rewrite-field">{t('components.max')}
        <input disabled={off} max={5} min={1} onChange={(event) => set({ max_per_turn: Math.max(1, Math.min(5, Number(event.target.value) || 1)) })} type="number" value={config.max_per_turn} />
        <small>{t('components.maxHint')}</small>
      </label>
      <label className="rewrite-field">{t('components.onInvalid')}
        <select disabled={off} onChange={(event) => set({ on_invalid: event.target.value as ComponentsConfig['on_invalid'] })} value={config.on_invalid}>
          {(['text', 'retry', 'drop'] as const).map((value) => <option key={value} value={value}>{t(`components.policy.${value}`)}</option>)}
        </select>
      </label>
      <label className="rewrite-field">{t('components.template')}
        <textarea disabled={off} maxLength={500} onChange={(event) => set({ text_template: event.target.value })} rows={2} value={config.text_template} />
        <small>{t('components.templateHint')}</small>
      </label>
      <h3>{t('components.previewTitle', { count: preview?.channels.length ?? 0 })}</h3>
      <p className="usage-hint">{t('components.previewHint')}</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {preview && preview.channels.length === 0 && <p className="wizard-muted">{t('components.noChannels')}</p>}
      <ul className="oc-injection-list">
        {preview?.channels.map((channel) => (
          <li key={channel.channel_id}>
            <button aria-expanded={open === channel.channel_id} className="oc-injection-row" onClick={() => setOpen(open === channel.channel_id ? null : channel.channel_id)} type="button">
              <strong>{channel.name}</strong>
              <span>{channel.type_name} · {channel.published ? t('components.published', { version: channel.published_version }) : t('components.unpublished')}</span>
              <span className="component-compat">{channel.components.map((item) => <span className={`mode-${item.mode}`} key={item.trigger_tag}>{item.trigger_tag}</span>)}</span>
            </button>
            {open === channel.channel_id && (
              <div className="oc-injection-body">
                {channel.prompt ? <pre className="channel-prompt">{channel.prompt}</pre> : <p className="wizard-muted">{channel.published ? t('components.nothingInjected') : t('components.notPublished')}</p>}
                <Link to="/channels">{t('components.goChannel')}</Link>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
