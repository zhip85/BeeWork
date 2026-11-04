import { useCallback, useEffect, useState } from 'react'
import { Code2, Globe, Pencil, Plus, Rocket, RotateCw, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { ApiRequestError, apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { ChannelDialog } from './ChannelDialog'
import type { AssistantOption, Channel } from './ChannelDialog'

/** Website channels (T-040): list, create/edit, publish (through the approval policy), embed code, enable/disable. */
export function ChannelsPage({ tenant, assistantId, embedded = false }: { tenant: TenantMembership; assistantId?: string; embedded?: boolean }) {
  const { t } = useTranslation('channels')
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const can = (permission: string) => writable && tenant.permissions.includes(permission)

  const [channels, setChannels] = useState<Channel[] | null>(null)
  const [assistants, setAssistants] = useState<AssistantOption[]>([])
  const [search, setSearch] = useState('')
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
    apiRequest<{ items: Channel[] }>(`/api/v1/channels?${params}`, {}, tenantId)
      .then((result) => { if (active) { setChannels(result.items); setError('') } })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [tenantId, assistantId, search, reload, t])

  useEffect(() => {
    let active = true
    apiRequest<{ items: AssistantOption[] }>('/api/v1/assistants?limit=100', {}, tenantId)
      .then((result) => active && setAssistants(result.items.map((item) => ({ id: item.id, name: item.name }))))
      .catch(() => active && setAssistants([]))
    return () => { active = false }
  }, [tenantId])

  async function publish(channel: Channel, replacePending = false) {
    setError('')
    try {
      const result = await apiRequest<{ pending_approval?: boolean; approval_id?: string }>(`/api/v1/channels/${channel.id}/publish`, {
        method: 'POST', body: JSON.stringify({ replace_pending: replacePending }),
      }, tenantId)
      setNotice(result.pending_approval ? { text: t('publish.submitted'), approvalId: result.approval_id } : { text: t('publish.done') })
      setReplacing(null)
      refresh()
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'approval.pending_exists') setReplacing(channel)
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
      {!embedded && <div className="assistants-toolbar"><input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></div>}
      {notice && <p className="assistants-notice" role="status">{notice.text} {notice.approvalId && <Link to={`/approvals?id=${notice.approvalId}`}>{t('publish.viewApproval')}</Link>}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {channels && channels.length === 0 && <p className="assistants-empty-filtered">{t('empty')}</p>}
      {channels && channels.length > 0 && (
        <div className="assistants-table-wrap">
          <table className="assistants-table">
            <thead><tr><th>{t('columns.channel')}</th><th>{t('columns.assistant')}</th><th>{t('columns.environment')}</th><th>{t('columns.version')}</th><th>{t('columns.status')}</th><th /></tr></thead>
            <tbody>
              {channels.map((channel) => (
                <tr key={channel.id}>
                  <td><span className="channel-name"><Globe size={14} /><strong>{channel.name}</strong></span><small className="platform-tenant-slug">{t('type.website')}</small></td>
                  <td>{channel.assistant_name ?? <span className="role-text">{t('assistantMissing')}</span>}</td>
                  <td>{t(`environments.${channel.environment}`)}</td>
                  <td>
                    {channel.published_version}
                    {channel.has_unpublished_changes && <small className="tenant-status-note">{t('unpublished')}</small>}
                    {channel.pending_approval_id && <Link className="assistant-status in_progress" to={`/approvals?id=${channel.pending_approval_id}`}>{t('inApproval')}</Link>}
                  </td>
                  <td><span className={`approval-status ${channel.status === 'active' ? 'approved' : channel.status === 'disabled' ? 'rejected' : 'withdrawn'}`}>{t(`status.${channel.status}`)}</span></td>
                  <td className="row-actions">
                    {can('channels:edit') && <button aria-label={t('edit')} className="icon-button" onClick={() => setEditing(channel)} title={t('edit')} type="button"><Pencil size={14} /></button>}
                    {can('channels:publish') && <button aria-label={t('publish.action')} className="icon-button" onClick={() => void publish(channel)} title={t('publish.action')} type="button"><Rocket size={14} /></button>}
                    {channel.embed_code && <button aria-label={t('embed.action')} className="icon-button" onClick={() => setEmbedFor(channel)} title={t('embed.action')} type="button"><Code2 size={14} /></button>}
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
      {embedFor?.embed_code && (
        <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={() => setEmbedFor(null)}>
          <div aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('close')} className="modal-close" onClick={() => setEmbedFor(null)} type="button"><X size={18} /></button>
            <h2>{t('embed.title')}</h2>
            <p>{t('embed.body')}</p>
            <textarea aria-label={t('embed.title')} className="embed-code" onFocus={(event) => event.target.select()} readOnly rows={3} value={embedFor.embed_code} />
            <p className="wizard-muted">{t('embed.origins', { origins: embedFor.allowed_origins.join(', ') })}</p>
            <div className="confirm-actions">
              <button className="primary-button" onClick={() => { void navigator.clipboard?.writeText(embedFor.embed_code ?? ''); setNotice({ text: t('embed.copied') }); setEmbedFor(null) }} type="button">{t('embed.copy')}</button>
            </div>
          </div>
        </div>
      )}
      {deleting && <ConfirmDialog body={t('deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)} onConfirm={() => remove(deleting)} title={t('delete')} />}
      {rotating && <ConfirmDialog body={t('rotate.body', { name: rotating.name })} danger onCancel={() => setRotating(null)} onConfirm={async () => { await action(rotating, 'rotate-key', 'rotate.done'); setRotating(null) }} title={t('rotate.action')} />}
      {replacing && <ConfirmDialog body={t('publish.replaceBody')} onCancel={() => setReplacing(null)} onConfirm={() => publish(replacing, true)} title={t('publish.action')} />}
    </section>
  )
}
