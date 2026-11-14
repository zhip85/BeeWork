import { useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ComponentRenderer } from './ComponentRenderer'
import { parseSample } from './renderer-utils'
import { CHANNEL_CODES, MODES, TYPES } from './types'
import type { ComponentContent, ComponentType, Compatibility, Mode, TypeDefaults } from './types'

export type EditorValue = ComponentContent & { status: string; version?: string }

function pretty(raw: string): string | null {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return null
  }
}

/** The component editor shared by the tenant library and the platform's template page (CHN-045, PLT-112). */
export function ComponentEditor({
  scope,
  initial,
  defaults,
  statuses,
  onClose,
  onSave,
}: {
  scope: 'tenant' | 'platform'
  initial: (ComponentContent & { status: string; version: string; template_name?: string | null; template_version?: string | null }) | null
  defaults: TypeDefaults | null
  statuses: string[]
  onClose: () => void
  onSave: (value: EditorValue) => Promise<void>
}) {
  const { t } = useTranslation('components')
  const first = defaults?.card
  const [value, setValue] = useState<EditorValue>(() => initial ?? {
    name_zh: t('editor.newName'), name_en: '', type: 'card', description: '', trigger_tag: first?.trigger_tag ?? 'product_card',
    data_schema: first?.data_schema ?? '{}', sample_data: first?.sample_data ?? '{}', compatibility: first?.compatibility ?? {}, status: statuses[1] ?? statuses[0], version: '0.1',
  })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)
  const set = <K extends keyof EditorValue>(key: K, next: EditorValue[K]) => setValue((current) => ({ ...current, [key]: next }))

  const sample = useMemo(() => parseSample(value.sample_data), [value.sample_data])

  function switchType(next: ComponentType) {
    const base = defaults?.[next]
    setValue((current) => ({ ...current, type: next, ...(base ? { trigger_tag: base.trigger_tag, data_schema: base.data_schema, sample_data: base.sample_data } : {}) }))
  }

  function format(key: 'data_schema' | 'sample_data') {
    const formatted = pretty(value[key])
    if (formatted === null) { setNotice(''); setError(t('editor.badJson')); return }
    setError('')
    setNotice(t('editor.formatted'))
    set(key, formatted)
  }

  async function submit() {
    setSaving(true)
    setError('')
    try {
      await onSave(value)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('editor.saveFailed'))
      setSaving(false)
    }
  }

  const compatibility: Compatibility = value.compatibility
  const origin = initial?.template_name
    ? t('editor.fromTemplate', { version: initial.template_version ?? '' })
    : scope === 'platform' ? t('editor.platformNote') : t('editor.selfBuilt')

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <div aria-labelledby="component-editor-title" aria-modal="true" className="create-modal component-editor" onClick={(event) => event.stopPropagation()} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <small>{scope === 'platform' ? t('editor.platformKicker') : t('editor.tenantKicker')}</small>
        <h2 id="component-editor-title">{value.name_zh || t('editor.newName')}</h2>
        <p className="wizard-muted">{origin}</p>
        <div className="component-editor-body">
          <div className="component-editor-form">
            <fieldset>
              <legend>{t('editor.basics')}</legend>
              <label>{t('editor.nameZh')} *<input maxLength={80} onChange={(event) => set('name_zh', event.target.value)} value={value.name_zh} /></label>
              <label>{t('editor.nameEn')}<input maxLength={80} onChange={(event) => set('name_en', event.target.value)} value={value.name_en} /></label>
              <label>{t('editor.type')}
                <select onChange={(event) => switchType(event.target.value as ComponentType)} value={value.type}>
                  {TYPES.map((kind) => <option key={kind} value={kind}>{t(`types.${kind}`)}</option>)}
                </select>
                <small>{t('editor.typeHint')}</small>
              </label>
              <label>{t('editor.description')}<input maxLength={300} onChange={(event) => set('description', event.target.value)} value={value.description} /></label>
              <label>{t('editor.tag')} *<input maxLength={40} onChange={(event) => set('trigger_tag', event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} value={value.trigger_tag} /><small>{t('editor.tagHint')}</small></label>
              <label>{t('editor.status')}
                <select onChange={(event) => set('status', event.target.value)} value={value.status}>{statuses.map((status) => <option key={status} value={status}>{t(`status.${status}`)}</option>)}</select>
              </label>
              {scope === 'platform' && <label>{t('editor.version')}<input onChange={(event) => set('version', event.target.value)} pattern="[0-9]+\.[0-9]+" value={value.version ?? ''} /><small>{t('editor.versionHint')}</small></label>}
            </fieldset>
            <fieldset>
              <legend>{t('editor.schema')}</legend>
              <textarea aria-label={t('editor.schema')} onChange={(event) => set('data_schema', event.target.value)} rows={9} spellCheck={false} value={value.data_schema} />
              <button className="secondary-button" onClick={() => format('data_schema')} type="button">{t('editor.format')}</button>
              <small>{t('editor.schemaHint')}</small>
            </fieldset>
            <fieldset>
              <legend>{t('editor.compat')}</legend>
              <p className="wizard-muted">{t('editor.compatHint')}</p>
              {CHANNEL_CODES.map((code) => (
                <div className="component-compat-row" key={code}>
                  <span>{t(`channelNames.${code}`)}</span>
                  <span className="component-modes" role="radiogroup" aria-label={t(`channelNames.${code}`)}>
                    {MODES.map((mode: Mode) => (
                      <button aria-checked={(compatibility[code] ?? 'off') === mode} className={`mode-${mode}${(compatibility[code] ?? 'off') === mode ? ' active' : ''}`} key={mode} onClick={() => set('compatibility', { ...compatibility, [code]: mode })} role="radio" type="button">{t(`modes.${mode}`)}</button>
                    ))}
                  </span>
                </div>
              ))}
            </fieldset>
          </div>
          <div className="component-editor-preview">
            <fieldset>
              <legend>{t('editor.sample')}</legend>
              <textarea aria-label={t('editor.sample')} onChange={(event) => set('sample_data', event.target.value)} rows={9} spellCheck={false} value={value.sample_data} />
              <button className="secondary-button" onClick={() => format('sample_data')} type="button">{t('editor.format')}</button>
            </fieldset>
            <fieldset>
              <legend>{t('editor.preview')}</legend>
              {sample === null ? <p className="form-error">{t('editor.badJson')}</p> : <ComponentRenderer data={sample} kind={value.type} placeholder={t('editor.placeholder')} />}
            </fieldset>
          </div>
        </div>
        {notice && <p className="assistants-notice" role="status">{notice}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <small className="wizard-muted">{scope === 'platform' ? t('editor.platformFoot') : t('editor.tenantFoot')}</small>
          <button className="secondary-button" onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={saving || !value.name_zh.trim() || !value.trigger_tag.trim()} onClick={() => void submit()} type="button">{saving ? t('saving') : t('save')}</button>
        </div>
      </div>
    </div>
  )
}
