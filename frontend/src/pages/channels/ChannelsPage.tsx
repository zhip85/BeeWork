import { useCallback, useEffect, useState } from 'react'
import { Code2, Globe, Pencil, Plus, Rocket, RotateCw, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { ApiRequestError, apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { ChannelDialog } from './ChannelDialog'
import type { AssistantOption, Channel, ChannelType } from './ChannelDialog'
import { PublishDialog } from './PublishDialog'
import { ComponentLibrary } from '../components/ComponentLibrary'

/** Channels (T-040/T-041): five types, list, wizard, publish dialog (through the approval policy), integration code, enable/disable, types tab. */
export function ChannelsPage({ tenant, assistantId, embedded = false }: { tenant: TenantMembership; assistantId?: string; embedded?: boolean }) {
  const { t } = useTranslation('channels')
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const can = (permission: string) => writable && tenant.permissions.includes(permission)

  const [channels, setChannels] = useState<Channel[] | null>(null)
  const [assistants, setAssistants] = useState<AssistantOption[]>([])
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [tab, setTab] = useState<'instances' | 'components' | 'types'>('instances')
  const [types, setTypes] = useState<ChannelType[]>([])
  const [publishing, setPublishing] = useState<Channel | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState<{ text: string; approvalId?: string } | null>(null)
  const [editing, setEditing] = useState<Channel | 'new' | null>(null)
  const [embedFor, setEmbedFor] = useState<Channel | null>(null)
  const [deleting, setDeleting] = useState<Channel | null>(null)
  const [rotating, setRotating] = useState<Channel | null>(null)
  const [replacing, setReplacing] = useState<Channel | null>(null)
  const [reload, setReload] = useState(0)

  const refresh = useCallback(() => setReload((value) => value + 1), [])

  useEffect(() => {
    let active = true
    const params = new URLSearchParams()
    if (assistantId) params.set('assistant_id', assistantId)
    if (search.trim()) params.set('q', search.trim())
    if (typeFilter) params.set('type', typeFilter)
    apiRequest<{ items: Channel[] }>(`/api/v1/channels?${params}`, {}, tenantId)
      .then((result) => { if (active) { setChannels(result.items); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [tenantId, assistantId, search, typeFilter, reload, t])

  useEffect(() => {
    let active = true
    apiRequest<{ items: AssistantOption[] }>('/api/v1/assistants?limit=100', {}, tenantId)
      .then((result) => active && setAssistants(result.items.map((item) => ({ id: item.id, name: item.name }))))
      .catch(() => active && setAssistants([]))
    return () => { active = false }
  }, [tenantId])

  useEffect(() => {
    let active = true
    apiRequest<{ items: ChannelType[] }>('/api/v1/channel-types', {}, tenantId)
      .then((result) => active && setTypes(result.items))
      .catch(() => active && setTypes([]))
    return () => { active = false }
  }, [tenantId, reload])

  async function publish(channel: Channel, replacePending = false) {
    setError('')
    try {
      const result = await apiRequest<{ pending_approval?: boolean; approval_id?: string }>(`/api/v1/channels/${channel.id}/publish`, {
        method: 'POST', body: JSON.stringify({ replace_pending: replacePending }),
      }, tenantId)
      setNotice(result.pending_approval ? { text: t('publish.submitted'), approvalId: result.approval_id } : { text: t('publish.done') })
      setReplacing(null)
      setPublishing(null)
      refresh()
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'approval.pending_exists') { setPublishing(null); setReplacing(channel) }
      else setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function action(channel: Channel, path: string, doneKey: string) {
    try {
      await apiRequest(`/api/v1/channels/${channel.id}/${path}`, { method: 'POST' }, tenantId)
      setNotice({ text: t(doneKey) })
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function remove(channel: Channel) {
    try {
      await apiRequest(`/api/v1/channels/${channel.id}`, { method: 'DELETE' }, tenantId)
      setNotice({ text: t('deleted') })
      setDeleting(null)
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
      setDeleting(null)
    }
  }

  const environments = tenant.environments

  return (
    <section className={embedded ? 'channels-embedded' : 'settings-page'}>
      <div className="page-heading">
        <div>{embedded ? <h2>{t('embeddedTitle')}</h2> : <h1>{t('title')}</h1>}<p>{t('subtitle')}</p></div>
        {can('channels:edit') && <button className="primary-button" disabled={assistants.length === 0} onClick={() => setEditing('new')} type="button"><Plus size={14} /> {t('new')}</button>}
      </div>
      {!embedded && (
        <div className="channel-tabs" role="tablist">
          {(['instances', 'components', 'types'] as const).map((value) => (
            <button aria-selected={tab === value} className={tab === value ? 'active' : ''} key={value} onClick={() => setTab(value)} role="tab" type="button">{t(`tabs.${value}`)}</button>
          ))}
        </div>
      )}
      {!embedded && tab === 'instances' && (
        <div className="assistants-toolbar">
          <input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} />
          <select aria-label={t('fields.type')} onChange={(event) => setTypeFilter(event.target.value)} value={typeFilter}>
            <option value="">{t('allTypes')}</option>
            {types.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}
          </select>
        </div>
      )}
      {tab === 'components' && !embedded && <ComponentLibrary canDelete={can('widgets:delete')} canEdit={can('widgets:edit')} tenantId={tenantId} />}
      {tab === 'types' && !embedded && (
        <div className="channel-type-grid">
          {types.map((item) => (
            <article className={`channel-type-card${item.available ? '' : ' unavailable'}`} key={item.code}>
              <header><Globe size={16} /><strong>{item.name}</strong><small>v{item.version}</small></header>
              <p>{item.available ? t('typeStatus.enabled') : t(`unavailable.${item.unavailable_reason ?? 'disabled'}`)}</p>
              <p className="wizard-muted">{t('typeParams', { params: item.connection_params.map((param) => param.label).join(' · ') })}</p>
              <p className="channel-tags">{item.capabilities.map((tag) => <span key={tag}>{tag}</span>)}</p>
            </article>
          ))}
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice.text} {notice.approvalId && <Link to={`/approvals?id=${notice.approvalId}`}>{t('publish.viewApproval')}</Link>}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {tab === 'instances' && channels && channels.length === 0 && <p className="assistants-empty-filtered">{t('empty')}</p>}
      {tab === 'instances' && channels && channels.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('columns.channel')}</th><th>{t('columns.assistant')}</th><th>{t('columns.environment')}</th><th>{t('columns.version')}</th><th>{t('columns.status')}</th><th /></tr></thead>
            <tbody>
              {channels.map((channel) => (
                <tr key={channel.id}>
                  <td><span className="channel-name"><Globe size={14} /><strong>{channel.name}</strong></span><small className="platform-tenant-slug">{channel.type_name}</small></td>
                  <td>{channel.assistant_name ?? <span className="role-text">{t('assistantMissing')}</span>}</td>
                  <td>{t(`environments.${channel.environment}`)}</td>
                  <td>
                    {channel.published_version}
                    {channel.has_unpublished_changes && <small className="tenant-status-note">{t('unpublished')}</small>}
                    {channel.pending_approval_id && <Link className="assistant-status in_progress" to={`/approvals?id=${channel.pending_approval_id}`}>{t('inApproval')}</Link>}
                  </td>
                  <td><span className={`approval-status ${channel.status === 'active' ? 'approved' : channel.status === 'disabled' ? 'rejected' : channel.status === 'error' ? 'failed' : 'withdrawn'}`} title={channel.health_error ?? undefined}>{t(`status.${channel.status}`)}</span></td>
                  <td className="row-actions">
                    {can('channels:edit') && <button aria-label={t('edit')} className="icon-button" onClick={() => setEditing(channel)} title={t('edit')} type="button"><Pencil size={14} /></button>}
                    {can('channels:publish') && <button aria-label={t('publish.action')} className="icon-button" onClick={() => setPublishing(channel)} title={t('publish.action')} type="button"><Rocket size={14} /></button>}
                    {channel.integration && <button aria-label={t('embed.action')} className="icon-button" onClick={() => setEmbedFor(channel)} title={t('embed.action')} type="button"><Code2 size={14} /></button>}
                    {can('channels:publish') && channel.status === 'active' && <button className="text-button" onClick={() => void action(channel, 'disable', 'disabledToast')} type="button">{t('disable')}</button>}
                    {can('channels:publish') && channel.status === 'disabled' && <button className="text-button" onClick={() => void action(channel, 'enable', 'enabledToast')} type="button">{t('enable')}</button>}
                    {can('channels:edit') && <button aria-label={t('rotate.action')} className="icon-button" onClick={() => setRotating(channel)} title={t('rotate.action')} type="button"><RotateCw size={14} /></button>}
                    {can('channels:delete') && <button aria-label={t('delete')} className="icon-button" onClick={() => setDeleting(channel)} title={t('delete')} type="button"><Trash2 size={14} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <ChannelDialog
          assistants={assistants}
          channel={editing === 'new' ? null : editing}
          environments={environments}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); setNotice({ text: t('saved') }); refresh() }}
          presetAssistantId={assistantId}
          tenantId={tenantId}
        />
      )}
      {embedFor?.integration && (
        <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={() => setEmbedFor(null)}>
          <div aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('close')} className="modal-close" onClick={() => setEmbedFor(null)} type="button"><X size={18} /></button>
            <h2>{t('embed.title')}</h2>
            <p>{t(`embed.body.${embedFor.integration.kind}`)}</p>
            <textarea aria-label={t('embed.title')} className="embed-code" onFocus={(event) => event.target.select()} readOnly rows={4} value={embedFor.integration.code} />
            {embedFor.allowed_origins.length > 0 && <p className="wizard-muted">{t('embed.origins', { origins: embedFor.allowed_origins.join(', ') })}</p>}
            <div className="confirm-actions">
              <button className="primary-button" onClick={() => { void navigator.clipboard?.writeText(embedFor.integration?.code ?? ''); setNotice({ text: t('embed.copied') }); setEmbedFor(null) }} type="button">{t('embed.copy')}</button>
            </div>
          </div>
        </div>
      )}
      {publishing && <PublishDialog channel={publishing} onCancel={() => setPublishing(null)} onConfirm={() => publish(publishing)} tenantId={tenantId} />}
      {deleting && <ConfirmDialog body={t('deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('delete')} />}
      {rotating && <ConfirmDialog body={t('rotate.body', { name: rotating.name })} danger onCancel={() => setRotating(null)} onConfirm={async () => { await action(rotating, 'rotate-key', 'rotate.done'); setRotating(null) }} title={t('rotate.action')} />}
      {replacing && <ConfirmDialog body={t('publish.replaceBody')} onCancel={() => setReplacing(null)} onConfirm={() => publish(replacing, true)} title={t('publish.action')} />}
    </section>
  )
}
