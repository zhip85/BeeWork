import { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { ComponentEditor } from '../components/ComponentEditor'
import type { EditorValue } from '../components/ComponentEditor'
import { ComponentRenderer } from '../components/ComponentRenderer'
import { parseSample } from '../components/renderer-utils'
import { CHANNEL_CODES } from '../components/types'
import type { TypeDefaults, WidgetTemplate } from '../components/types'

/** Platform widget templates (PLT-110–114): the SaaS library tenants copy from. The same editor as the tenant's, with a status and a version. */
export function PlatformWidgetTemplatesPage() {
  const { t } = useTranslation('components')
  const [templates, setTemplates] = useState<WidgetTemplate[]>([])
  const [editing, setEditing] = useState<WidgetTemplate | 'new' | null>(null)
  const [deleting, setDeleting] = useState<WidgetTemplate | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const refresh = useCallback(() => setReload((value) => value + 1), [])

  useEffect(() => {
    let active = true
    apiRequest<{ items: WidgetTemplate[] }>('/api/v1/platform/widget-templates')
      .then((result) => { if (active) { setTemplates(result.items); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [reload])

  // A platform admin has no tenant, so the per-type defaults are taken from the templates themselves.
  const defaults = useMemo<TypeDefaults | null>(() => {
    if (templates.length === 0) return null
    const make = (kind: 'card' | 'comparison' | 'timeline' | 'form') => {
      const base = templates.find((item) => item.type === kind)
      return { trigger_tag: base?.trigger_tag ?? kind, data_schema: base?.data_schema ?? '{}', sample_data: base?.sample_data ?? '{}', compatibility: base?.compatibility ?? {} }
    }
    return { card: make('card'), comparison: make('comparison'), timeline: make('timeline'), form: make('form') }
  }, [templates])

  async function save(value: EditorValue) {
    const target = editing && editing !== 'new' ? editing : null
    await apiRequest(target ? `/api/v1/platform/widget-templates/${target.id}` : '/api/v1/platform/widget-templates', { method: target ? 'PUT' : 'POST', body: JSON.stringify(value) })
    setEditing(null)
    setNotice(t('saved'))
    refresh()
  }

  async function remove(template: WidgetTemplate) {
    try {
      await apiRequest(`/api/v1/platform/widget-templates/${template.id}`, { method: 'DELETE' })
      setNotice(t('deleted'))
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('failed'))
    }
    setDeleting(null)
  }

  return (
    <section className="settings-page">
      <div className="page-heading">
        <div><h1>{t('platform.title')}</h1><p>{t('platform.subtitle')}</p></div>
        <button className="primary-button" onClick={() => setEditing('new')} type="button"><Plus size={14} /> {t('platform.new')}</button>
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="component-grid">
        {templates.map((template) => (
          <article className="component-card" key={template.id} onClick={() => setEditing(template)} onKeyDown={(event) => { if ((event.key === 'Enter' || event.key === ' ') && event.target === event.currentTarget) { event.preventDefault(); setEditing(template) } }} role="button" tabIndex={0}>
            <div className="component-card-preview"><ComponentRenderer data={parseSample(template.sample_data)} kind={template.type} /></div>
            <p className="component-badges"><span>{t('badge.saas')}</span><span>{t(`types.${template.type}`)}</span>{template.copies > 0 && <span>{t('platform.copies', { count: template.copies })}</span>}</p>
            <strong>{template.name_zh}</strong>
            <small>{template.description}</small>
            <p className="component-compat">{CHANNEL_CODES.map((code) => <span className={`mode-${template.compatibility[code] ?? 'off'}`} key={code}>{t(`channelNames.${code}`)}</span>)}</p>
            <footer>
              <span>v{template.version} · {t(`status.${template.status}`)}</span>
              <button className="text-button" disabled={template.copies > 0} onClick={(event) => { event.stopPropagation(); setDeleting(template) }} title={template.copies > 0 ? t('platform.onlyDeprecate') : undefined} type="button">{t('delete')}</button>
            </footer>
          </article>
        ))}
      </div>
      {editing && <ComponentEditor defaults={defaults} initial={editing === 'new' ? null : editing} onClose={() => setEditing(null)} onSave={save} scope="platform" statuses={['published', 'draft', 'deprecated']} />}
      {deleting && <ConfirmDialog body={t('platform.deleteBody', { name: deleting.name_zh })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('delete')} />}
    </section>
  )
}
