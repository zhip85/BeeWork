import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Bot, MoreHorizontal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { AssistantBasicsPanel } from './AssistantBasicsPanel'
import { MountSettings } from './MountSettings'
import { AssistantNav } from './AssistantNav'
import type { NavKey } from './AssistantNav'
import { CapabilityPicker } from './CapabilityPicker'
import type { Selection } from './CapabilityPicker'
import { DependenciesTab } from './DependenciesTab'
import { ChannelsPage } from '../channels/ChannelsPage'
import { OpenApiCard } from '../channels/OpenApiCard'
import { PublishDialog } from './PublishDialog'
import { ConversationsPage } from './ConversationsPage'
import { TestCasesTab } from './TestCasesTab'
import { VersionsPage } from './VersionsPage'
import { TestConsole } from './TestConsole'
import type { Assistant, OrganizationOption } from './types'

type CheckIssue = { code: string; severity: 'blocker' | 'high' | 'info'; message: string; target: string }
type Checks = { issues: CheckIssue[]; blockers: number; high_impact: number; info: number }
type MountEntry = { id: string; kind: string; name: string; state: 'available' | 'disabled' | 'deleted'; meta?: { reason?: string | null } }
type Mounts = { total: number; groups: { kind: string; items: MountEntry[] }[]; mountable_kinds: string[] }

const TABS = ['overview', 'basics', 'capabilities', 'channels', 'tests', 'versions', 'conversations', 'dependencies'] as const
type Tab = (typeof TABS)[number]
// Orchestration is its own set of pages: an old ``?tab=orchestration`` link goes to its first stage.
const MOVED: Record<string, (id: string) => string> = { orchestration: (id) => `/assistants/${id}/oc/pii` }

export function AssistantDetailPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('detail')
  const { t: ta } = useTranslation('assistants')
  const { assistantId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const tenantId = tenant.id
  const writable = tenant.access_mode === 'full'
  const can = (permission: string) => writable && tenant.permissions.includes(permission)

  const [assistant, setAssistant] = useState<Assistant | null>(null)
  const [checks, setChecks] = useState<Checks | null>(null)
  const [mounts, setMounts] = useState<Mounts | null>(null)
  const [teamError, setTeamError] = useState('')
  const [teamBusy, setTeamBusy] = useState(false)
  const [organizations, setOrganizations] = useState<OrganizationOption[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [pending, setPending] = useState<'delete' | 'disable' | null>(null)
  const [mounting, setMounting] = useState(false)
  const [versionOptions, setVersionOptions] = useState<{ id: string; version: string; is_live: boolean }[]>([])

  const requested = params.get('tab') as Tab | null
  const tab: Tab = requested && TABS.includes(requested) ? requested : 'overview'

  useEffect(() => {
    const moved = params.get('tab') ? MOVED[params.get('tab') as string] : undefined
    if (moved) navigate(moved(assistantId), { replace: true })
  }, [params, assistantId, navigate])

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<Assistant>(`/api/v1/assistants/${assistantId}`, {}, tenantId),
      apiRequest<Checks>(`/api/v1/assistants/${assistantId}/checks`, {}, tenantId),
      apiRequest<Mounts>(`/api/v1/assistants/${assistantId}/mounts`, {}, tenantId),
    ])
      .then(([current, result, mounted]) => {
        if (!active) return
        setAssistant(current)
        setChecks(result)
        setMounts(mounted)
        setError('')
      })
      .catch((requestError: Error) => active && setError(requestError.message || t('loadFailed')))
    return () => { active = false }
  }, [assistantId, tenantId, reload, t])

  useEffect(() => {
    let active = true
    apiRequest<{ organizations: OrganizationOption[] }>('/api/v1/organizations', {}, tenantId).then((result) => active && setOrganizations(result.organizations)).catch(() => undefined)
    apiRequest<{ items: { id: string; version: string; is_live: boolean }[] }>(`/api/v1/assistants/${assistantId}/versions`, {}, tenantId).then((result) => active && setVersionOptions(result.items)).catch(() => undefined)
    return () => { active = false }
  }, [tenantId, assistantId, reload])

  const refresh = useCallback(() => setReload((value) => value + 1), [])
  async function switchTeam(enabled: boolean) {
    if (!assistant) return
    setTeamBusy(true)
    setTeamError('')
    try {
      await apiRequest(`/api/v1/assistants/${assistant.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: assistant.name, description: assistant.description, category: assistant.category, icon: assistant.icon, color: assistant.color,
          visibility: assistant.visibility, visible_org_ids: assistant.visible_org_ids, row_version: assistant.row_version,
          config: { ...assistant.config, multi_agent: { enabled } },
        }),
      }, tenantId)
      refresh()
    } catch (failure) {
      setTeamError(failure instanceof Error ? failure.message : ta('picker.loadFailed'))
    } finally {
      setTeamBusy(false)
    }
  }
  const setTab = (next: Tab) => setParams(next === 'overview' ? {} : { tab: next }, { replace: true })
  const goTo = (path: string) => navigate(path)

  async function act(path: string, doneKey: string) {
    try {
      await apiRequest(`/api/v1/assistants/${assistantId}/${path}`, { method: 'POST' }, tenantId)
      setNotice(ta(doneKey))
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function copy() {
    try {
      const created = await apiRequest<Assistant>(`/api/v1/assistants/${assistantId}/copy`, { method: 'POST' }, tenantId)
      navigate(`/assistants/${created.id}`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
    }
  }

  async function remove() {
    await apiRequest(`/api/v1/assistants/${assistantId}`, { method: 'DELETE' }, tenantId)
    navigate('/assistants')
  }

  if (!assistant) {
    return (
      <section className="settings-page assistant-detail">
        <Link className="back-link" to="/assistants"><ArrowLeft size={14} /> {t('back')}</Link>
        {error ? <p className="form-error" role="alert">{error} <button className="text-button" onClick={refresh} type="button">{ta('retry')}</button></p> : <p>{t('loading')}</p>}
      </section>
    )
  }

  const editable = assistant.can_edit
  const canEditBasics = editable && can('assistants:edit')

  const menu: { key: string; label: string; run: () => void; danger?: boolean }[] = []
  if (editable && can('assistants:edit')) menu.push({ key: 'copy', label: ta('menu.copy'), run: () => void copy() })
  if (editable && can('assistants:publish') && assistant.status !== 'draft') {
    menu.push({ key: 'toggle', label: assistant.status === 'published' ? ta('menu.disable') : ta('menu.enable'), run: () => assistant.status === 'published' ? setPending('disable') : void act('enable', 'toast.enabled') })
  }
  if (editable && can('assistants:delete') && assistant.status !== 'published') menu.push({ key: 'delete', label: ta('menu.delete'), run: () => setPending('delete'), danger: true })

  return (
    <section className="settings-page assistant-detail">
      <Link className="back-link" to="/assistants"><ArrowLeft size={14} /> {t('back')}</Link>
      <header className="detail-header">
        <span className="assistant-icon"><Bot size={22} /></span>
        <div>
          <h1>{assistant.name}<span className={`assistant-status ${assistant.status}`}>{ta(`status.${assistant.status}`)}</span>{assistant.pending_approval_id && <Link className="assistant-status in_progress" to={`/approvals?id=${assistant.pending_approval_id}`}>{ta('status.pendingApproval')}</Link>}</h1>
          <p>{t('subtitle', { org: assistant.organization_name, version: assistant.draft_version })}</p>
        </div>
        <div className="detail-actions">
          <button className="secondary-button" onClick={() => setTab('tests')} type="button">{t('test')}</button>
          {editable && can('assistants:publish') && assistant.status !== 'disabled' && <button className="primary-button" onClick={() => setPublishing(true)} type="button">{t('publish')}</button>}
          {menu.length > 0 && (
            <span className="assistant-menu">
              <button aria-expanded={menuOpen} aria-haspopup="menu" aria-label={ta('menu.more')} className="icon-button" onClick={() => setMenuOpen(!menuOpen)} type="button"><MoreHorizontal size={16} /></button>
              {menuOpen && <ul role="menu">{menu.map((entry) => <li key={entry.key} role="none"><button className={entry.danger ? 'danger' : ''} onClick={() => { setMenuOpen(false); entry.run() }} role="menuitem" type="button">{entry.label}</button></li>)}</ul>}
            </span>
          )}
        </div>
      </header>

      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="orchestration-layout assistant-workspace">
      <AssistantNav active={tab as NavKey} capabilityCount={mounts?.total} go={goTo} assistantId={assistant.id} showOc={tenant.permissions.includes('orchestration:view')} />
      <div className="assistant-workspace-content">

      {tab === 'overview' && (
        <div className="detail-overview-split">
        <div className="detail-overview">
          <div className="detail-card">
            <h2>{t('checks.title')}</h2>
            {checks && checks.issues.length === 0 && <p className="assistants-notice">{t('checks.allClear')}</p>}
            {checks && checks.issues.length > 0 && (
              <ul className="check-list">
                {checks.issues.map((issue) => (
                  <li className={`check-${issue.severity}`} key={issue.code + issue.message}>
                    <span className="check-badge">{t(`checks.severity.${issue.severity}`)}</span>
                    <span>{issue.message}</span>
                    {issue.target === 'basic' && canEditBasics && <button className="text-button" onClick={() => setTab('basics')} type="button">{t('checks.fix')}</button>}
                    {issue.target === 'capabilities' && <button className="text-button" onClick={() => setTab('capabilities')} type="button">{t('checks.fix')}</button>}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p className="wizard-muted">{t('metricsPending')}</p>
        </div>
        <TestConsole assistant={assistant} key={assistant.id + ':' + assistant.live_version_id} tenant={tenant} />
        </div>
      )}

      {tab === 'basics' && <AssistantBasicsPanel assistant={assistant} canEdit={canEditBasics} onSaved={() => { setNotice(ta('toast.saved')); refresh() }} organizations={organizations} tenant={tenant} />}

      {tab === 'capabilities' && (
        <div className="detail-card">
          <header className="detail-card-head">
            <h2>{t('capabilities.title', { count: mounts?.total ?? 0 })}</h2>
            <button
              className="secondary-button"
              disabled={!(can('assistants:edit') && assistant.can_edit && (mounts?.mountable_kinds.length ?? 0) > 0)}
              onClick={() => setMounting(true)}
              title={(mounts?.mountable_kinds.length ?? 0) > 0 ? undefined : t('capabilities.soonHint')}
              type="button"
            >{t('capabilities.mount')}</button>
          </header>
          <label className="wizard-toggle team-toggle">
            <input checked={Boolean(assistant.config.multi_agent?.enabled)} disabled={teamBusy || !(can('assistants:edit') && assistant.can_edit)} onChange={(event) => void switchTeam(event.target.checked)} type="checkbox" />
            <span><strong>{t('capabilities.team.title')}</strong><small>{t('capabilities.team.hint')}</small></span>
          </label>
          {teamError && <p className="form-error" role="alert">{teamError}</p>}
          {mounts && mounts.total === 0 && <p className="assistants-empty-filtered">{t('capabilities.empty')}</p>}
          {mounts && mounts.groups.map((group) => (
            <div className="mount-group" key={group.kind}>
              <h3>{t(`capabilities.kinds.${group.kind}`)} · {group.items.length}</h3>
              <ul>
                {group.items.map((item) => (
                  <li className={item.state === 'available' ? '' : 'mount-invalid'} key={item.id}>
                    <span>{item.name || item.id}</span>
                    <span className="mount-state">{t(`capabilities.state.${item.state}`)}{item.meta?.reason ? ` · ${t(`capabilities.reason.${item.meta.reason}`)}` : ''}</span>
                    {(group.kind === 'mcp' || group.kind === 'skill' || group.kind === 'agent') && item.state === 'available' && (
                      <MountSettings assistant={assistant} canEdit={can('assistants:edit') && assistant.can_edit} item={{ id: item.id, kind: group.kind, name: item.name }} onSaved={refresh} tenantId={tenantId} />
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
          <p className="wizard-muted">{t('capabilities.draftNote')}</p>
          <p className="wizard-muted">{t('capabilities.soonHint')}</p>
        </div>
      )}

      {mounting && (
        <MountDialog
          assistant={assistant}
          mounts={mounts}
          onClose={() => setMounting(false)}
          onSaved={() => { setMounting(false); setNotice(ta('picker.saved')); setReload((value) => value + 1) }}
          teamEnabled={Boolean(assistant.config.multi_agent?.enabled)}
          tenantId={tenantId}
        />
      )}

      {tab === 'versions' && <div className="detail-card"><VersionsPage assistantId={assistant.id} embedded tenant={tenant} /></div>}
      {tab === 'conversations' && <div className="detail-card"><ConversationsPage assistantId={assistant.id} embedded tenantId={tenantId} /></div>}
      {tab === 'tests' && <TestCasesTab assistant={assistant} tenant={tenant} versions={versionOptions} />}
      {tab === 'channels' && (
        <div className="detail-channels">
          <ChannelsPage assistantId={assistant.id} embedded tenant={tenant} />
          <OpenApiCard assistantId={assistant.id} />
        </div>
      )}
      {tab === 'dependencies' && <DependenciesTab assistantId={assistant.id} tenantId={tenantId} />}

      </div>
      </div>

      {publishing && (
        <PublishDialog
          assistant={assistant}
          onClose={() => setPublishing(false)}
          onPublished={(version) => {
            setPublishing(false)
            setNotice(t('published', { version }))
            refresh()
          }}
          onSubmitted={() => {
            setPublishing(false)
            setNotice(t('submitted'))
            refresh()
          }}
          tenantId={tenantId}
        />
      )}

      {pending && (
        <ConfirmDialog
          body={ta(pending === 'delete' ? 'confirm.deleteBody' : 'confirm.disableBody', { name: assistant.name })}
          danger={pending === 'delete'}
          onCancel={() => setPending(null)}
          onConfirm={async () => {
            try {
              if (pending === 'delete') await remove()
              else await act('disable', 'toast.disabled')
            } catch (requestError) {
              setError(requestError instanceof Error ? requestError.message : t('loadFailed'))
            } finally {
              setPending(null)
            }
          }}
          title={ta(pending === 'delete' ? 'confirm.deleteTitle' : 'confirm.disableTitle')}
        />
      )}
    </section>
  )
}

function MountDialog({
  assistant,
  mounts,
  teamEnabled,
  tenantId,
  onClose,
  onSaved,
}: {
  assistant: Assistant
  mounts: Mounts | null
  teamEnabled: boolean
  tenantId: string
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation('assistants')
  const current = mounts?.groups.flatMap((group) => group.items) ?? []
  const [selected, setSelected] = useState<Selection>(() => ({ ...(assistant.config.mounts ?? {}) }) as Selection)
  const [names, setNames] = useState<Record<string, string>>(Object.fromEntries(current.map((item) => [item.id, item.name])))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [warning, setWarning] = useState<string[] | null>(null)

  // AST-065: priority rules of the intent stage that point into a capability about to be unmounted.
  async function referencedRules(): Promise<string[]> {
    const before = new Set(Object.values(assistant.config.mounts ?? {}).flat() as string[])
    const after = new Set(Object.values(selected).flat())
    const removed = [...before].filter((id) => !after.has(id))
    const found = await Promise.all(removed.map((id) => apiRequest<{ rules: string[] }>(`/api/v1/assistants/${assistant.id}/oc/routing/references?source_id=${id}`, {}, tenantId).then((data) => data.rules).catch(() => [] as string[])))
    return [...new Set(found.flat())]
  }

  // Settings of a capability that is no longer mounted go with it.
  function keptSettings() {
    const mounted = new Set(Object.values(selected).flat())
    return Object.fromEntries(Object.entries(assistant.config.mount_settings ?? {}).filter(([id]) => mounted.has(id)))
  }

  async function save() {
    setSaving(true)
    setError('')
    try {
      if (warning === null) {
        const rules = await referencedRules()
        if (rules.length > 0) {
          setWarning(rules)
          setSaving(false)
          return
        }
      }
      await apiRequest(
        `/api/v1/assistants/${assistant.id}`,
        {
          method: 'PUT',
          body: JSON.stringify({
            name: assistant.name,
            description: assistant.description,
            category: assistant.category,
            icon: assistant.icon,
            color: assistant.color,
            visibility: assistant.visibility,
            visible_org_ids: assistant.visible_org_ids,
            config: { ...assistant.config, mounts: { ...(assistant.config.mounts ?? {}), ...selected }, mount_settings: keptSettings() },
            row_version: assistant.row_version,
          }),
        },
        tenantId,
      )
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('picker.loadFailed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" onClick={onClose} role="presentation">
      <div aria-labelledby="mount-title" aria-modal="true" className="create-modal mount-dialog" onClick={(event) => event.stopPropagation()} role="dialog">
        <h2 id="mount-title">{t('picker.title')}</h2>
        <CapabilityPicker hiddenKinds={teamEnabled ? [] : ['agent']} names={names} onChange={(next, nextNames) => { setSelected(next); setNames(nextNames); setWarning(null) }} selected={selected} tenantId={tenantId} />
        {warning && <p className="form-error" role="alert">{t('picker.rulesWarning', { rules: warning.join('、') })}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" disabled={saving} onClick={onClose} type="button">{t('picker.cancel')}</button>
          <button className="primary-button" disabled={saving} onClick={() => void save()} type="button">{saving ? t('picker.saving') : warning ? t('picker.saveAnyway') : t('picker.save')}</button>
        </div>
      </div>
    </div>
  )
}
