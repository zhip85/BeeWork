import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ArrowLeft, CheckCircle2, ChevronDown, ClipboardCheck, ShieldCheck, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { AgentIcon } from './AgentIcon'
import type { AgentChecks, AgentDetail } from './types'

const TABS = { agent: ['instructions'], chatflow: ['flow', 'session', 'preview'], workflow: ['flow', 'io', 'triggers', 'run'] } as const

/** Editor shell (AGT-020–027): header, status row, save / make available / disable, the check bar and the
 * guardrail notice. The body of each type arrives with T-061–T-064. */
export function AgentEditorPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('agents')
  const { agentId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const [agent, setAgent] = useState<AgentDetail | null>(null)
  const [name, setName] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState((location.state as { notice?: string } | null)?.notice ?? '')
  const [busy, setBusy] = useState(false)
  const [checks, setChecks] = useState<AgentChecks | null>(null)
  const [checksOpen, setChecksOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirm, setConfirm] = useState<'leave' | 'disable' | null>(null)

  const dirty = agent !== null && name.trim() !== agent.name
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && agent?.can_edit === true
  const canPublish = writable && agent?.can_publish === true

  const apply = useCallback((next: AgentDetail) => { setAgent(next); setName(next.name) }, [])

  useEffect(() => {
    setLoading(true)
    apiRequest<AgentDetail>(`/api/v1/agents/${agentId}`, {}, tenant.id)
      .then((detail) => { apply(detail); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [agentId, tenant.id, apply, t])

  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [menuOpen])

  const listPath = agent && agent.kind !== 'agent' ? `/agents?type=${agent.kind}` : '/agents'

  async function loadChecks(): Promise<AgentChecks | null> {
    try {
      const result = await apiRequest<AgentChecks>(`/api/v1/agents/${agentId}/checks`, {}, tenant.id)
      setChecks(result)
      return result
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      return null
    }
  }

  async function toggleChecks() {
    if (checksOpen) { setChecksOpen(false); return }
    setChecksOpen(true)
    await loadChecks()
  }

  async function saveDraft(): Promise<AgentDetail | null> {
    if (!agent) return null
    try {
      const saved = await apiRequest<AgentDetail>(`/api/v1/agents/${agent.id}`, {
        method: 'PUT',
        body: JSON.stringify({ name: name.trim(), row_version: agent.row_version }),
      }, tenant.id)
      apply(saved)
      return saved
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      return null
    }
  }

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError('')
    setNotice('')
    try { await action() } finally { setBusy(false) }
  }

  const onSave = () => run(async () => {
    const saved = await saveDraft()
    if (saved) { setNotice(t('draftSaved')); if (checksOpen) await loadChecks() }
  })

  const onMakeAvailable = () => run(async () => {
    if (dirty && !(await saveDraft())) return
    try {
      const next = await apiRequest<AgentDetail>(`/api/v1/agents/${agentId}/make-available`, { method: 'POST' }, tenant.id)
      apply(next)
      setNotice(t('madeAvailable'))
      if (checksOpen) await loadChecks()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setChecksOpen(true)
      await loadChecks()
    }
  })

  const transition = (action: 'disable' | 'enable') => run(async () => {
    try {
      const next = await apiRequest<AgentDetail>(`/api/v1/agents/${agentId}/${action}`, { method: 'POST' }, tenant.id)
      apply(next)
      setNotice(t(action === 'disable' ? 'disabledNotice' : 'enabledNotice'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  })

  function back() {
    if (dirty) setConfirm('leave')
    else navigate(listPath)
  }

  if (loading) return <section className="agents-editor"><p>{t('loading')}</p></section>
  if (!agent) return <section className="agents-editor"><p className="form-error" role="alert">{error || t('notFound')}</p><button className="back-link" onClick={() => navigate('/agents')} type="button"><ArrowLeft size={15} /> {t('backTo', { kind: t('kind.agent') })}</button></section>

  const kindName = t(`kind.${agent.kind}`)
  if (agent.config_corrupted) {
    return (
      <section className="agents-editor">
        <button className="back-link" onClick={() => navigate(listPath)} type="button"><ArrowLeft size={15} /> {t('backTo', { kind: kindName })}</button>
        <p className="form-error" role="alert"><AlertTriangle size={14} /> {t('corrupted')}</p>
      </section>
    )
  }

  return (
    <section className="agents-editor">
      <header className="agents-editor-head">
        <button aria-label={t('back')} className="icon-button" onClick={back} type="button"><ArrowLeft size={17} /></button>
        <span className={`agents-icon ${agent.kind}`}><AgentIcon name={agent.icon} /></span>
        <div className="agents-editor-title">
          <input aria-label={t('name')} disabled={!canEdit} maxLength={50} onChange={(event) => setName(event.target.value)} value={name} />
          <small>
            {agent.is_team ? t('team') : kindName} · {dirty ? t('unsaved') : t('saved')}
            <span className={`assistant-status agents-status-${agent.status}`}>{t(`status.${agent.status}`)}</span>
            {agent.has_draft_changes && <em>{t('draftChanges')}</em>}
          </small>
        </div>
        <div className="agents-editor-actions">
          <button className="secondary-button" onClick={() => void toggleChecks()} type="button"><ClipboardCheck size={15} /> {t('checkConfig')}</button>
          {canEdit && <button className="secondary-button" disabled={busy || !dirty || !name.trim()} onClick={() => void onSave()} type="button">{t('saveDraft')}</button>}
          {canPublish && agent.status !== 'disabled' && <button className="primary-button" disabled={busy || !name.trim()} onClick={() => void onMakeAvailable()} type="button">{t('makeAvailable')}</button>}
          {canPublish && agent.status !== 'draft' && (
            <span className="assistant-menu">
              <button aria-expanded={menuOpen} aria-haspopup="menu" aria-label={t('more')} className="icon-button" onClick={(event) => { event.stopPropagation(); setMenuOpen(!menuOpen) }} type="button"><ChevronDown size={16} /></button>
              {menuOpen && (
                <ul role="menu">
                  <li role="none">
                    {agent.status === 'disabled'
                      ? <button onClick={() => void transition('enable')} role="menuitem" type="button">{t('enable')}</button>
                      : <button className="danger" onClick={() => setConfirm('disable')} role="menuitem" type="button">{t('disable')}</button>}
                  </li>
                </ul>
              )}
            </span>
          )}
        </div>
      </header>
      {checksOpen && (
        <div className={`agents-checks ${checks?.ok ? 'ok' : 'warn'}`} role="status">
          {checks?.ok ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
          <span>{checks === null ? t('loading') : checks.ok ? t('checksPassed') : <><strong>{t('checksPending')}</strong> {checks.issues.map((issue) => issue.message).join('；')}</>}</span>
          <button aria-label={t('close')} className="icon-button" onClick={() => setChecksOpen(false)} type="button"><X size={15} /></button>
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="agents-editor-body">
        <div className="detail-card">
          <h2>{t('editorTabs')}</h2>
          <ul className="agents-tab-list">
            {TABS[agent.kind].map((tab) => <li key={tab}>{t(`tab.${tab}`)}</li>)}
          </ul>
          <p className="agents-pending">{t('bodyPending', { kind: kindName })}</p>
          {agent.kind === 'agent' && <p className="agents-guardrail"><ShieldCheck size={15} /> {t('guardrail')}</p>}
        </div>
        {agent.reference_count > 0 && <p className="agents-pending">{t('referencedBy', { count: agent.reference_count })}</p>}
      </div>
      {confirm === 'leave' && (
        <ConfirmDialog body={t('discardBody')} danger onCancel={() => setConfirm(null)} onConfirm={() => navigate(listPath)} title={t('discardTitle')} />
      )}
      {confirm === 'disable' && (
        <ConfirmDialog
          body={agent.reference_count > 0 ? t('disableBodyReferenced', { count: agent.reference_count }) : t('disableBody')}
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={async () => { setConfirm(null); await transition('disable') }}
          title={t('disableTitle', { kind: kindName })}
        />
      )}
    </section>
  )
}
