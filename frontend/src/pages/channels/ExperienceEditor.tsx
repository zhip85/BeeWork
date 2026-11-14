import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

export type Level = 'platform' | 'tenant' | 'assistant' | 'instance'
const LEVELS: Level[] = ['platform', 'tenant', 'assistant', 'instance']

type Field = { key: string; kind: 'color' | 'choice' | 'text' | 'switch'; options: (string | number)[] }
type Values = Record<string, string | number | boolean>
type View = {
  channel_type: string
  channel_type_name: string
  fields: Field[]
  layers: Record<Level, Values>
  values: Values
  sources: Record<string, Level>
  greeting: string
  row_version: number | null
}

/** Merge exactly like the server: platform → tenant → assistant (its greeting is the welcome text unless it sets one) → instance. */
function merge(view: View, current: Level, draft: Values): { values: Values; sources: Record<string, Level> } {
  const layers = { ...view.layers, [current]: draft } as Record<Level, Values>
  const assistant = { ...layers.assistant }
  if (view.greeting.trim() && assistant.welcome === undefined) assistant.welcome = view.greeting.trim()
  const stack: [Level, Values][] = [['platform', layers.platform], ['tenant', layers.tenant], ['assistant', assistant], ['instance', layers.instance]]
  const values: Values = {}
  const sources: Record<string, Level> = {}
  for (const field of view.fields) {
    for (const [level, layer] of stack) {
      const value = layer[field.key]
      if (value !== undefined && value !== null && value !== '') { values[field.key] = value; sources[field.key] = level }
    }
  }
  return { values, sources }
}

/**
 * The experience editor (CHN-030–035): one editor for the four layers. It shows the chain, every setting with where its value comes
 * from and “restore inheritance” where this layer overrides, and a live preview. Published channels keep their frozen values (CHN-035).
 */
export function ExperienceEditor({
  level,
  channelType,
  tenantId,
  assistantId,
  channelId,
  onClose,
  onSaved,
}: {
  level: Level
  channelType: string
  tenantId?: string
  assistantId?: string
  channelId?: string
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation('channels')
  const [view, setView] = useState<View | null>(null)
  const [draft, setDraft] = useState<Values>({})
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let active = true
    const load = async () => {
      if (level === 'platform') {
        // A platform administrator has no tenant: the platform layer is the type's default.
        const list = await apiRequest<{ items: { code: string; name: string; default_experience: Values; experience_fields: Field[] }[] }>('/api/v1/platform/channel-types')
        const kind = list.items.find((item) => item.code === channelType)
        if (!kind) throw new Error(t('experience.loadFailed'))
        return { channel_type: kind.code, channel_type_name: kind.name, fields: kind.experience_fields, layers: { platform: kind.default_experience, tenant: {}, assistant: {}, instance: {} }, values: kind.default_experience, sources: {}, greeting: '', row_version: null } as View
      }
      const params = new URLSearchParams()
      if (level === 'instance' && channelId) params.set('channel_id', channelId)
      else if (level === 'assistant' && assistantId) params.set('assistant_id', assistantId)
      return apiRequest<View>(`/api/v1/experience/${channelType}?${params}`, {}, tenantId)
    }
    load()
      .then((value) => { if (active) { setView(value); setDraft({ ...value.layers[level] }) } })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [level, channelType, tenantId, assistantId, channelId, t])

  const merged = useMemo(() => (view ? merge(view, level, draft) : null), [view, level, draft])

  async function save() {
    if (!view) return
    setSaving(true)
    setError('')
    try {
      if (level === 'platform') await apiRequest(`/api/v1/platform/channel-types/${channelType}`, { method: 'PATCH', body: JSON.stringify({ default_experience: draft }) })
      else if (level === 'tenant') await apiRequest(`/api/v1/channel-types/${channelType}/tenant-experience`, { method: 'PUT', body: JSON.stringify({ values: draft }) }, tenantId)
      else if (level === 'assistant') await apiRequest(`/api/v1/assistants/${assistantId}/channel-experience/${channelType}`, { method: 'PUT', body: JSON.stringify({ values: draft, row_version: view.row_version }) }, tenantId)
      else await apiRequest(`/api/v1/channels/${channelId}/experience`, { method: 'PUT', body: JSON.stringify({ values: draft, row_version: view.row_version }) }, tenantId)
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('saveFailed'))
      setSaving(false)
    }
  }

  const set = (key: string, value: string | number | boolean) => setDraft((current) => ({ ...current, [key]: value }))
  const restore = (key: string) => setDraft((current) => { const next = { ...current }; delete next[key]; return next })
  const values = merged?.values ?? {}
  const color = typeof values.primary_color === 'string' ? values.primary_color : '#6558d9'
  const radius = typeof values.radius === 'number' ? values.radius : 12
  const dark = values.theme === 'dark'

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <div aria-labelledby="experience-title" aria-modal="true" className="create-modal experience-editor" onClick={(event) => event.stopPropagation()} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <small>{t(`experience.kicker.${level}`)}</small>
        <h2 id="experience-title">{t('experience.title', { type: view?.channel_type_name ?? channelType })}</h2>
        <ol aria-label={t('experience.chain')} className="experience-chain">
          {LEVELS.map((item, index) => <li aria-current={item === level} className={item === level ? 'current' : ''} key={item}>{String(index + 1).padStart(2, '0')} {t(`experience.level.${item}`)}</li>)}
        </ol>
        {error && <p className="form-error" role="alert">{error}</p>}
        {!view && !error && <p className="wizard-muted">{t('loading')}</p>}
        {view && merged && (
          <div className="experience-body">
            <div className="experience-fields">
              {view.fields.map((field) => {
                const own = draft[field.key] !== undefined
                const source = merged.sources[field.key] ?? 'platform'
                const value = values[field.key]
                return (
                  <div className="experience-row" key={field.key}>
                    <label htmlFor={`exp-${field.key}`}>{t(`experience.fields.${field.key}`)}</label>
                    <span className={`experience-source source-${source}`}>{t(`experience.level.${source}`)}</span>
                    {field.kind === 'color' && <input id={`exp-${field.key}`} onChange={(event) => set(field.key, event.target.value)} type="color" value={typeof value === 'string' ? value : '#6558d9'} />}
                    {field.kind === 'choice' && (
                      <select id={`exp-${field.key}`} onChange={(event) => set(field.key, typeof field.options[0] === 'number' ? Number(event.target.value) : event.target.value)} value={String(value ?? '')}>
                        {field.options.map((option) => <option key={option} value={String(option)}>{field.key === 'radius' ? `${option}` : t(`experience.options.${field.key}.${option}`, String(option))}</option>)}
                      </select>
                    )}
                    {field.kind === 'text' && <textarea id={`exp-${field.key}`} maxLength={200} onChange={(event) => set(field.key, event.target.value)} rows={2} value={typeof value === 'string' ? value : ''} />}
                    {field.kind === 'switch' && <input checked={value === true} id={`exp-${field.key}`} onChange={(event) => set(field.key, event.target.checked)} type="checkbox" />}
                    {own && <button className="text-button" onClick={() => restore(field.key)} type="button">{t('experience.restore')}</button>}
                  </div>
                )
              })}
            </div>
            <aside aria-label={t('experience.preview')} className="channel-preview">
              <strong>{t('experience.preview')}</strong>
              {channelType === 'open-api' ? (
                <pre>{JSON.stringify({ message: '…', widgets: values.cards === false ? undefined : [], quick_replies: values.quick_replies === false ? undefined : [], stream: values.streaming === true }, null, 2)}</pre>
              ) : (
                <div className="channel-phone" style={{ borderRadius: radius + 6, background: dark ? '#1c1c22' : undefined, color: dark ? '#eee' : undefined }}>
                  <header style={{ background: color }}>{view.channel_type_name}</header>
                  <p className="bubble bot">{typeof values.welcome === 'string' && values.welcome ? values.welcome : t('preview.defaultWelcome')}</p>
                  <p className="bubble user">{t('preview.user')}</p>
                  {values.cards !== false && <p className="bubble bot">{t('experience.sampleCard')}</p>}
                  {values.quick_replies !== false && <p className="experience-quick"><span>{t('experience.quick1')}</span><span>{t('experience.quick2')}</span></p>}
                </div>
              )}
            </aside>
          </div>
        )}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={!view || saving} onClick={() => void save()} type="button">{saving ? t('saving') : t('experience.save')}</button>
        </div>
      </div>
    </div>
  )
}
