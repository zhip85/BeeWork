import { useCallback, useEffect, useState } from 'react'
import { Copy, Plus, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { ComponentEditor } from './ComponentEditor'
import type { EditorValue } from './ComponentEditor'
import { ComponentRenderer } from './ComponentRenderer'
import { parseSample } from './renderer-utils'
import { CHANNEL_CODES } from './types'
import type { ComponentContent, TenantComponent, TypeDefaults, WidgetTemplate } from './types'

type SyncPreview = { from_version: string; to_version: string; changes: { field: string; from: unknown; to: unknown }[] }

function CompatRow({ item }: { item: ComponentContent }) {
  const { t } = useTranslation('components')
  return (
    <p className="component-compat">{CHANNEL_CODES.map((code) => (
      <span className={`mode-${item.compatibility[code] ?? 'off'}`} key={code} title={t(`modes.${item.compatibility[code] ?? 'off'}`)}>{t(`channelNames.${code}`)}</span>
    ))}</p>
  )
}

/** The component library (CHN-040–046): the tenant's own components and the read-only SaaS templates to copy from. */
export function ComponentLibrary({ tenantId, canEdit, canDelete }: { tenantId: string; canEdit: boolean; canDelete: boolean }) {
  const { t } = useTranslation('components')
  const [segment, setSegment] = useState<'mine' | 'saas'>('mine')
  const [items, setItems] = useState<TenantComponent[]>([])
  const [templates, setTemplates] = useState<WidgetTemplate[]>([])
  const [defaults, setDefaults] = useState<TypeDefaults | null>(null)
  const [editing, setEditing] = useState<TenantComponent | 'new' | null>(null)
  const [syncing, setSyncing] = useState<{ item: TenantComponent; preview: SyncPreview } | null>(null)
  const [deleting, setDeleting] = useState<TenantComponent | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const refresh = useCallback(() => setReload((value) => value + 1), [])

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<{ items: TenantComponent[] }>('/api/v1/components', {}, tenantId),
      apiRequest<{ items: WidgetTemplate[] }>('/api/v1/widget-templates', {}, tenantId),
      apiRequest<TypeDefaults>('/api/v1/component-defaults', {}, tenantId),
    ])
      .then(([mine, saas, kinds]) => { if (active) { setItems(mine.items); setTemplates(saas.items); setDefaults(kinds); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [tenantId, reload])

  const fail = (requestError: unknown) => setError(requestError instanceof Error ? requestError.message : t('failed'))

  async function save(value: EditorValue) {
    const body = { ...value, row_version: editing && editing !== 'new' ? editing.row_version : undefined }
    await apiRequest(editing && editing !== 'new' ? `/api/v1/components/${editing.id}` : '/api/v1/components', { method: editing && editing !== 'new' ? 'PUT' : 'POST', body: JSON.stringify(body) }, tenantId)
    setEditing(null)
    setNotice(t('saved'))
    refresh()
  }

  async function copy(template: WidgetTemplate) {
    try {
      const created = await apiRequest<TenantComponent>(`/api/v1/components/from-template/${template.id}`, { method: 'POST' }, tenantId)
      setNotice(t('copied'))
      setSegment('mine')
      refresh()
      setEditing(created)
    } catch (requestError) { fail(requestError) }
  }

  async function openSync(item: TenantComponent) {
    try {
      setSyncing({ item, preview: await apiRequest<SyncPreview>(`/api/v1/components/${item.id}/sync-preview`, {}, tenantId) })
    } catch (requestError) { fail(requestError) }
  }

  async function confirmSync() {
    if (!syncing) return
    try {
      await apiRequest(`/api/v1/components/${syncing.item.id}/sync`, { method: 'POST' }, tenantId)
      setNotice(t('synced'))
      setSyncing(null)
      refresh()
    } catch (requestError) { fail(requestError); setSyncing(null) }
  }

  async function remove(item: TenantComponent) {
    try {
      await apiRequest(`/api/v1/components/${item.id}`, { method: 'DELETE' }, tenantId)
      setNotice(t('deleted'))
      refresh()
    } catch (requestError) { fail(requestError) }
    setDeleting(null)
  }

  return (
    <div className="component-library">
      <div className="channel-tabs" role="tablist">
        {(['mine', 'saas'] as const).map((value) => (
          <button aria-selected={segment === value} className={segment === value ? 'active' : ''} key={value} onClick={() => setSegment(value)} role="tab" type="button">
            {t(`segments.${value}`, { count: value === 'mine' ? items.length : templates.length })}
          </button>
        ))}
        {segment === 'mine' && canEdit && <button className="primary-button" onClick={() => setEditing('new')} type="button"><Plus size={14} /> {t('new')}</button>}
      </div>
      <p className="wizard-muted">{t(`segmentHint.${segment}`)}</p>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {segment === 'mine' && items.length === 0 && <p className="assistants-empty-filtered">{t('emptyMine')}</p>}
      <div className="component-grid">
        {segment === 'mine' && items.map((item) => (
          <article className="component-card" key={item.id} onClick={() => canEdit && setEditing(item)} onKeyDown={(event) => { if (canEdit && (event.key === 'Enter' || event.key === ' ') && event.target === event.currentTarget) { event.preventDefault(); setEditing(item) } }} role="button" tabIndex={0}>
            <div className="component-card-preview"><ComponentRenderer data={parseSample(item.sample_data)} kind={item.type} /></div>
            <p className="component-badges">
              <span>{item.template_id ? t('badge.fromTemplate') : t('badge.selfBuilt')}</span><span>{t(`types.${item.type}`)}</span>
              {item.template_latest && <span className="update">{t('badge.updated', { version: item.template_latest })}</span>}
            </p>
            <strong>{item.name_zh}</strong>
            <small>{item.description}</small>
            <CompatRow item={item} />
            <footer>
              <span>v{item.version} · {t(`status.${item.status}`)}</span>
              <span className="row-actions">
                {item.template_latest && canEdit && <button className="text-button" onClick={(event) => { event.stopPropagation(); void openSync(item) }} type="button"><RefreshCw size={12} /> {t('sync')}</button>}
                {canDelete && <button className="text-button" disabled={!item.can_delete} onClick={(event) => { event.stopPropagation(); setDeleting(item) }} title={item.can_delete ? undefined : t('inUse', { names: item.used_by.join(', ') })} type="button">{t('delete')}</button>}
              </span>
            </footer>
          </article>
        ))}
        {segment === 'saas' && templates.map((template) => (
          <article className="component-card" key={template.id} onClick={() => canEdit && void copy(template)} onKeyDown={(event) => { if (canEdit && (event.key === 'Enter' || event.key === ' ') && event.target === event.currentTarget) { event.preventDefault(); void copy(template) } }} role="button" tabIndex={0}>
            <div className="component-card-preview"><ComponentRenderer data={parseSample(template.sample_data)} kind={template.type} /></div>
            <p className="component-badges"><span>{t('badge.saas')}</span><span>{t(`types.${template.type}`)}</span>{template.copies > 0 && <span>{t('badge.copies', { count: template.copies })}</span>}</p>
            <strong>{template.name_zh}</strong>
            <small>{template.description}</small>
            <CompatRow item={template} />
            <footer>
              <span>v{template.version} · {t('readOnly')}</span>
              {canEdit && <button className="text-button" onClick={(event) => { event.stopPropagation(); void copy(template) }} type="button"><Copy size={12} /> {t('copyToMine')}</button>}
            </footer>
          </article>
        ))}
      </div>
      {editing && (
        <ComponentEditor
          defaults={defaults}
          initial={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSave={save}
          scope="tenant"
          statuses={['enabled', 'draft', 'disabled']}
        />
      )}
      {syncing && (
        <ConfirmDialog
          body={t('syncBody', { fields: syncing.preview.changes.map((change) => change.field).join('、') })}
          onCancel={() => setSyncing(null)}
          onConfirm={confirmSync}
          title={t('syncTitle', { from: syncing.preview.from_version, to: syncing.preview.to_version })}
        />
      )}
      {deleting && <ConfirmDialog body={t('deleteBody', { name: deleting.name_zh })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('delete')} />}
    </div>
  )
}
