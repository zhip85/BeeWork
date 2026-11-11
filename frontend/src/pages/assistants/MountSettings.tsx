import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { Assistant } from './types'

type Tool = { name: string; description: string; read_only: boolean; selected: boolean }

/** Least privilege for one mounted capability (演进 PRD 7.6, 15.3): which of its tools this agent may use, and when to use it.
 * An agent can only be given tools the administrator ticked on the connection; the server re-checks every call. */
export function MountSettings({ assistant, item, tenantId, canEdit, onSaved }: {
  assistant: Assistant
  item: { id: string; kind: string; name: string }
  tenantId: string
  canEdit: boolean
  onSaved: () => void
}) {
  const { t } = useTranslation('assistants')
  const current = assistant.config.mount_settings?.[item.id]
  const [open, setOpen] = useState(false)
  const [tools, setTools] = useState<Tool[] | null>(null)
  const [allowed, setAllowed] = useState<Set<string>>(new Set(current?.tools ?? []))
  const [delegate, setDelegate] = useState(current?.delegate ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const hasTools = item.kind === 'mcp'
  const delegateRequired = item.kind === 'agent'

  async function toggle() {
    setOpen((value) => !value)
    if (!open && hasTools && tools === null) {
      try {
        const detail = await apiRequest<{ mcp: { tools: Tool[] } | null }>(`/api/v1/capabilities/${item.id}`, {}, tenantId)
        const offered = (detail.mcp?.tools ?? []).filter((tool) => tool.selected)
        setTools(offered)
        // “no restriction” (an empty list) shows as everything ticked
        if (!current?.tools?.length) setAllowed(new Set(offered.map((tool) => tool.name)))
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : t('mountSettings.failed'))
      }
    }
  }

  async function save() {
    setSaving(true)
    setError('')
    const everything = tools !== null && tools.every((tool) => allowed.has(tool.name))
    const entry = { tools: hasTools && !everything ? [...allowed] : [], delegate: delegate.trim() }
    try {
      await apiRequest(`/api/v1/assistants/${assistant.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: assistant.name, description: assistant.description, category: assistant.category, icon: assistant.icon, color: assistant.color,
          visibility: assistant.visibility, visible_org_ids: assistant.visible_org_ids, row_version: assistant.row_version,
          config: { ...assistant.config, mount_settings: { ...(assistant.config.mount_settings ?? {}), [item.id]: entry } },
        }),
      }, tenantId)
      setOpen(false)
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('mountSettings.failed'))
    } finally {
      setSaving(false)
    }
  }

  const summary = [
    hasTools ? (current?.tools?.length ? t('mountSettings.someTools', { count: current.tools.length }) : t('mountSettings.allTools')) : null,
    current?.delegate ? t('mountSettings.hasDelegate') : delegateRequired ? t('mountSettings.delegateMissing') : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="mount-settings">
      <button className="text-button" onClick={() => void toggle()} type="button">{t('mountSettings.title')}{summary && <small> · {summary}</small>}</button>
      {open && (
        <div className="mount-settings-body">
          {hasTools && (
            <fieldset disabled={!canEdit}>
              <legend>{t('mountSettings.tools')}</legend>
              {tools === null && !error && <small>{t('mountSettings.loading')}</small>}
              {tools?.length === 0 && <small>{t('mountSettings.noTools')}</small>}
              {tools?.map((tool) => (
                <label className="wizard-radio" key={tool.name}>
                  <input
                    checked={allowed.has(tool.name)}
                    onChange={(event) => setAllowed((previous) => { const next = new Set(previous); if (event.target.checked) next.add(tool.name); else next.delete(tool.name); return next })}
                    type="checkbox"
                  />
                  <span>{tool.name}{!tool.read_only && <em className="mount-write"> {t('mountSettings.write')}</em>}<small> {tool.description}</small></span>
                </label>
              ))}
              {tools && tools.length > 0 && allowed.size === 0 && <small className="form-error">{t('mountSettings.noneTicked')}</small>}
            </fieldset>
          )}
          <label>{t(delegateRequired ? 'mountSettings.delegateRequired' : 'mountSettings.delegate')}
            <textarea disabled={!canEdit} maxLength={500} onChange={(event) => setDelegate(event.target.value)} placeholder={t('mountSettings.delegateHint')} rows={2} value={delegate} />
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          {canEdit && <button className="primary-button" disabled={saving || (hasTools && tools !== null && tools.length > 0 && allowed.size === 0) || (delegateRequired && !delegate.trim())} onClick={() => void save()} type="button">{saving ? t('picker.saving') : t('picker.save')}</button>}
        </div>
      )}
    </div>
  )
}
