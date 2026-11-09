import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Bot, Copy, MoreHorizontal, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { AssistantWizard } from './AssistantWizard'
import { CapabilityPicker } from './CapabilityPicker'
import { ChannelsPage } from '../channels/ChannelsPage'
import { OpenApiCard } from '../channels/OpenApiCard'
import { PublishDialog } from './PublishDialog'
import { TestCasesTab } from './TestCasesTab'
import { TestConsole } from './TestConsole'
import type { Assistant, AssistantModelOptions, AssistantSummary, OrganizationOption } from './types'

type CheckIssue = { code: string; severity: 'blocker' | 'high' | 'info'; message: string; target: string }
type Checks = { issues: CheckIssue[]; blockers: number; high_impact: number; info: number }
type MountEntry = { id: string; kind: string; name: string; state: 'available' | 'disabled' | 'deleted' }
type Mounts = { total: number; groups: { kind: string; items: MountEntry[] }[]; mountable_kinds: string[] }

const TABS = ['overview', 'capabilities', 'orchestration', 'channels', 'versions', 'dependencies', 'tests', 'conversations'] as const
type Tab = (typeof TABS)[number]
const PREVIEW_LINES = 3

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
  const [models, setModels] = useState<AssistantModelOptions | null>(null)
  const [organizations, setOrganizations] = useState<OrganizationOption[]>([])
  const [quota, setQuota] = useState<AssistantSummary['quota'] | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const [wizard, setWizard] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [pending, setPending] = useState<'delete' | 'disable' | null>(null)
  const [mounting, setMounting] = useState(false)
  const [versionOptions, setVersionOptions] = useState<{ id: string; version: string; is_live: boolean }[]>([])

  const requested = params.get('tab') as Tab | null
  const tab: Tab = requested && TABS.includes(requested) ? requested : 'overview'

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
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenantId).then((result) => active && setModels(result)).catch(() => undefined)
    apiRequest<{ organizations: OrganizationOption[] }>('/api/v1/organizations', {}, tenantId).then((result) => active && setOrganizations(result.organizations)).catch(() => undefined)
    apiRequest<{ items: { id: string; version: string; is_live: boolean }[] }>(`/api/v1/assistants/${assistantId}/versions`, {}, tenantId).then((result) => active && setVersionOptions(result.items)).catch(() => undefined)
    apiRequest<AssistantSummary>('/api/v1/assistants/summary', {}, tenantId).then((result) => active && setQuota(result.quota)).catch(() => undefined)
    return () => { active = false }
  }, [tenantId, assistantId, reload])

  const refresh = useCallback(() => setReload((value) => value + 1), [])
  const setTab = (next: Tab) => setParams(next === 'overview' ? {} : { tab: next }, { replace: true })

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
        <Link className="text-button" to="/assistants"><ArrowLeft size={14} /> {t('back')}</Link>
        {error ? <p className="form-error" role="alert">{error} <button className="text-button" onClick={refresh} type="button">{ta('retry')}</button></p> : <p>{t('loading')}</p>}
      </section>
    )
  }

  const editable = assistant.can_edit
  const modelName = models?.models.find((model) => model.id === assistant.config.model_id)?.display_name
  const prompt = assistant.config.prompt
  const promptLines = prompt.split('\n')
  const promptPreview = promptLines.slice(0, PREVIEW_LINES).join('\n') + (promptLines.length > PREVIEW_LINES ? ' …' : '')

  const menu: { key: string; label: string; run: () => void; danger?: boolean }[] = []
  if (editable && can('assistants:edit')) menu.push({ key: 'copy', label: ta('menu.copy'), run: () => void copy() })
  if (editable && can('assistants:publish') && assistant.status !== 'draft') {
    menu.push({ key: 'toggle', label: assistant.status === 'published' ? ta('menu.disable') : ta('menu.enable'), run: () => assistant.status === 'published' ? setPending('disable') : void act('enable', 'toast.enabled') })
  }
  if (editable && can('assistants:delete') && assistant.status !== 'published') menu.push({ key: 'delete', label: ta('menu.delete'), run: () => setPending('delete'), danger: true })

  return (
    <section className="settings-page assistant-detail">
      <Link className="text-button" to="/assistants"><ArrowLeft size={14} /> {t('back')}</Link>
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

      <nav aria-label={t('tabsLabel')} className="detail-tabs" role="tablist">
        {TABS.map((key) => (
          <button aria-selected={tab === key} className={tab === key ? 'selected' : ''} key={key} onClick={() => setTab(key)} role="tab" type="button">
            {t(`tabs.${key}`)}{key === 'capabilities' && mounts ? ` ${mounts.total}` : ''}
          </button>
        ))}
      </nav>

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
                    {issue.target === 'basic' && editable && can('assistants:edit') && <button className="text-button" onClick={() => setWizard(true)} type="button">{t('checks.fix')}</button>}
                    {issue.target === 'capabilities' && <button className="text-button" onClick={() => setTab('capabilities')} type="button">{t('checks.fix')}</button>}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="detail-card">
            <h2>{t('path.title')}</h2>
            <ol className="config-path">
              <li><span>{t('path.identity')}</span><small>{t('path.identityHint')}</small>{editable && can('assistants:edit') && <button className="text-button" onClick={() => setWizard(true)} type="button">{ta('menu.edit')}</button>}</li>
              <li><span>{t('path.driver')}</span><small>{t('path.driverHint')}</small><em>{t('soon')}</em></li>
              <li><span>{t('path.capabilities')}</span><small>{t('path.capabilitiesHint', { count: mounts?.total ?? 0 })}</small><button className="text-button" onClick={() => setTab('capabilities')} type="button">{t('tabs.capabilities')}</button></li>
              <li><span>{t('path.oc')}</span><small>{t('path.ocHint')}</small><em>{t('soon')}</em></li>
              <li><span>{t('path.channels')}</span><small>{t('path.channelsHint')}</small><button className="text-button" onClick={() => setTab('channels')} type="button">{t('tabs.channels')}</button></li>
              <li><span>{t('path.tests')}</span><small>{t('path.testsHint')}</small><button className="text-button" onClick={() => setTab('tests')} type="button">{t('tabs.tests')}</button></li>
              <li><span>{t('path.publish')}</span><small>{t('path.publishHint')}</small><Link className="text-button" to={`/assistants/${assistant.id}/versions`}>{t('tabs.versions')}</Link></li>
            </ol>
          </div>

          <div className="detail-card">
            <header className="detail-card-head"><h2>{t('basic.title')}</h2>{editable && can('assistants:edit') && <button className="text-button" onClick={() => setWizard(true)} type="button">{ta('menu.edit')}</button>}</header>
            <dl className="basic-config">
              <dt>{t('basic.model')}</dt><dd>{modelName ?? (assistant.config.model_id ? t('basic.modelUnknown') : t('basic.none'))}</dd>
              <dt>{t('basic.driver')}</dt><dd>{t('basic.driverDefault')}</dd>
              <dt>{t('basic.language')}</dt><dd>{assistant.config.default_language === 'en' ? 'English' : '简体中文'}</dd>
              <dt>{t('basic.visibility')}</dt><dd>{t(`basic.visibilityValue.${assistant.visibility}`)}</dd>
              <dt>{t('basic.prompt')}</dt>
              <dd>
                {prompt ? <><pre className="prompt-preview">{promptPreview}</pre><button className="text-button" onClick={() => setShowPrompt(true)} type="button">{t('basic.viewPrompt')}</button></> : t('basic.none')}
              </dd>
            </dl>
          </div>
          <p className="wizard-muted">{t('metricsPending')}</p>
        </div>
        <TestConsole assistant={assistant} key={assistant.id + ':' + assistant.live_version_id} tenant={tenant} />
        </div>
      )}

      {tab === 'capabilities' && (
        <div className="detail-card">
          <header className="detail-card-head">
            <h2>{t('capabilities.title', { count: mounts?.total ?? 0 })}</h2>
            <button
              className="secondary-button"
              disabled={!(can('assistants:edit') && assistant.can_edit && mounts?.mountable_kinds.includes('kb'))}
              onClick={() => setMounting(true)}
              title={mounts?.mountable_kinds.includes('kb') ? undefined : t('capabilities.soonHint')}
              type="button"
            >{t('capabilities.mount')}</button>
          </header>
          {mounts && mounts.total === 0 && <p className="assistants-empty-filtered">{t('capabilities.empty')}</p>}
          {mounts && mounts.groups.map((group) => (
            <div className="mount-group" key={group.kind}>
              <h3>{t(`capabilities.kinds.${group.kind}`)} · {group.items.length}</h3>
              <ul>
                {group.items.map((item) => (
                  <li className={item.state === 'available' ? '' : 'mount-invalid'} key={item.id}>
                    <span>{item.name || item.id}</span>
                    <span className="mount-state">{t(`capabilities.state.${item.state}`)}</span>
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
          tenantId={tenantId}
        />
      )}

      {tab === 'versions' && (
        <div className="detail-card"><p>{t('versionsHint')}</p><Link className="primary-button" to={`/assistants/${assistant.id}/versions`}>{t('tabs.versions')}</Link></div>
      )}
      {tab === 'conversations' && (
        <div className="detail-card"><p>{t('conversationsHint')}</p><Link className="primary-button" to={`/assistants/${assistant.id}/conversations`}>{t('tabs.conversations')}</Link></div>
      )}
      {tab === 'tests' && <TestCasesTab assistant={assistant} tenant={tenant} versions={versionOptions} />}
      {tab === 'channels' && (
        <div className="detail-channels">
          <ChannelsPage assistantId={assistant.id} embedded tenant={tenant} />
          <OpenApiCard assistantId={assistant.id} />
        </div>
      )}
      {(tab === 'orchestration' || tab === 'dependencies') && (
        <div className="detail-card"><p className="assistants-empty-filtered">{t('soonTab', { name: t(`tabs.${tab}`) })}</p></div>
      )}

      {showPrompt && (
        <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={() => setShowPrompt(false)}>
          <div aria-labelledby="prompt-title" aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} role="dialog">
            <button aria-label={t('close')} className="modal-close" onClick={() => setShowPrompt(false)} type="button"><X size={18} /></button>
            <h2 id="prompt-title">{t('basic.promptTitle')}</h2>
            <pre className="version-lines">{prompt}</pre>
            <div className="confirm-actions">
              <button className="secondary-button" onClick={() => void navigator.clipboard?.writeText(prompt)} type="button"><Copy size={14} /> {t('basic.copy')}</button>
              {editable && can('assistants:edit') && <button className="primary-button" onClick={() => { setShowPrompt(false); setWizard(true) }} type="button">{ta('menu.edit')}</button>}
            </div>
          </div>
        </div>
      )}

      {wizard && (
        <AssistantWizard
          assistant={assistant}
          onClose={() => setWizard(false)}
          onSaved={(messageKey, close) => {
            setNotice(ta(messageKey))
            refresh()
            if (close) setWizard(false)
          }}
          organizations={organizations}
          quota={quota}
          tenant={tenant}
        />
      )}

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
  tenantId,
  onClose,
  onSaved,
}: {
  assistant: Assistant
  mounts: Mounts | null
  tenantId: string
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation('assistants')
  const current = mounts?.groups.find((group) => group.kind === 'kb')?.items ?? []
  const [ids, setIds] = useState<string[]>(assistant.config.mounts?.kb ?? [])
  const [names, setNames] = useState<Record<string, string>>(Object.fromEntries(current.map((item) => [item.id, item.name])))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function save() {
    setSaving(true)
    setError('')
    try {
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
            config: { ...assistant.config, mounts: { ...(assistant.config.mounts ?? {}), kb: ids } },
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
        <CapabilityPicker names={names} onChange={(next, nextNames) => { setIds(next); setNames(nextNames) }} selected={ids} tenantId={tenantId} />
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" disabled={saving} onClick={onClose} type="button">{t('picker.cancel')}</button>
          <button className="primary-button" disabled={saving} onClick={() => void save()} type="button">{saving ? t('picker.saving') : t('picker.save')}</button>
        </div>
      </div>
    </div>
  )
}
