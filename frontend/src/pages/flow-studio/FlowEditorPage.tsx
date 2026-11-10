import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ArrowLeft, CheckCircle2, ChevronDown, ClipboardCheck, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { FlowIcon } from './FlowIcon'
import type { FlowChecks, FlowDetail, FlowVersion } from './types'

const TABS = { chatflow: ['flow', 'session', 'preview'], workflow: ['flow', 'io', 'triggers', 'run'] } as const

/** Editor shell (AGT-020–027): header, status row, save / publish / disable, the check bar, the nodes overview with
 * invalid targets in red and the version history. The canvas and the per-type panels arrive with T-062–T-064. */
export function FlowEditorPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('flows')
  const { flowId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const [flow, setFlow] = useState<FlowDetail | null>(null)
  const [versions, setVersions] = useState<FlowVersion[]>([])
  const [name, setName] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState((location.state as { notice?: string } | null)?.notice ?? '')
  const [busy, setBusy] = useState(false)
  const [checks, setChecks] = useState<FlowChecks | null>(null)
  const [checksOpen, setChecksOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirm, setConfirm] = useState<'leave' | 'disable' | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [note, setNote] = useState('')

  const dirty = flow !== null && name.trim() !== flow.name
  const writable = tenant.access_mode === 'full'
  const canEdit = writable && flow?.can_edit === true
  const canPublish = writable && flow?.can_publish === true

  const apply = useCallback((next: FlowDetail) => { setFlow(next); setName(next.name) }, [])
  const loadVersions = useCallback(() => {
    apiRequest<FlowVersion[]>(`/api/v1/agents/${flowId}/versions`, {}, tenant.id).then(setVersions).catch(() => setVersions([]))
  }, [flowId, tenant.id])

  useEffect(() => {
    apiRequest<FlowDetail>(`/api/v1/agents/${flowId}`, {}, tenant.id)
      .then((detail) => { apply(detail); setError(''); loadVersions() })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [flowId, tenant.id, apply, loadVersions, t])

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

  const listPath = flow && flow.kind !== 'chatflow' ? `/flow-studio?type=${flow.kind}` : '/flow-studio'

  async function loadChecks(): Promise<void> {
    try {
      setChecks(await apiRequest<FlowChecks>(`/api/v1/agents/${flowId}/checks`, {}, tenant.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function toggleChecks() {
    if (checksOpen) { setChecksOpen(false); return }
    setChecksOpen(true)
    await loadChecks()
  }

  async function saveDraft(): Promise<FlowDetail | null> {
    if (!flow) return null
    try {
      const saved = await apiRequest<FlowDetail>(`/api/v1/agents/${flow.id}`, {
        method: 'PUT',
        body: JSON.stringify({ name: name.trim(), row_version: flow.row_version }),
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

  const onPublish = () => run(async () => {
    if (dirty && !(await saveDraft())) { setPublishing(false); return }
    try {
      const next = await apiRequest<FlowDetail>(`/api/v1/agents/${flowId}/publish`, { method: 'POST', body: JSON.stringify({ change_note: note.trim() }) }, tenant.id)
      apply(next)
      setNotice(t('published', { version: next.live_version ?? '' }))
      setNote('')
      loadVersions()
      if (checksOpen) await loadChecks()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setChecksOpen(true)
      await loadChecks()
    }
    setPublishing(false)
  })

  const transition = (action: 'disable' | 'enable') => run(async () => {
    try {
      apply(await apiRequest<FlowDetail>(`/api/v1/agents/${flowId}/${action}`, { method: 'POST' }, tenant.id))
      setNotice(t(action === 'disable' ? 'disabledNotice' : 'enabledNotice'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  })

  const duplicate = () => run(async () => {
    try {
      const copy = await apiRequest<FlowDetail>(`/api/v1/agents/${flowId}/duplicate`, { method: 'POST' }, tenant.id)
      navigate(`/flow-studio/${copy.id}`, { state: { notice: t('duplicated', { name: copy.name }) } })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  })

  function back() {
    if (dirty) setConfirm('leave')
    else navigate(listPath)
  }

  if (loading) return <section className="flows-editor"><p>{t('loading')}</p></section>
  if (!flow) return <section className="flows-editor"><p className="form-error" role="alert">{error || t('notFound')}</p><button className="back-link" onClick={() => navigate('/flow-studio')} type="button"><ArrowLeft size={15} /> {t('backTo', { kind: t('kind.chatflow') })}</button></section>

  const kindName = t(`kind.${flow.kind}`)
  if (flow.config_corrupted) {
    return (
      <section className="flows-editor">
        <button className="back-link" onClick={() => navigate(listPath)} type="button"><ArrowLeft size={15} /> {t('backTo', { kind: kindName })}</button>
        <p className="form-error" role="alert"><AlertTriangle size={14} /> {t('corrupted')}</p>
      </section>
    )
  }
  const nodes = flow.config?.nodes ?? []

  return (
    <section className="flows-editor">
      <header className="flows-editor-head">
        <button aria-label={t('back')} className="icon-button" onClick={back} type="button"><ArrowLeft size={17} /></button>
        <span className={`flows-icon ${flow.kind}`}><FlowIcon name={flow.icon} /></span>
        <div className="flows-editor-title">
          <input aria-label={t('name')} disabled={!canEdit} maxLength={50} onChange={(event) => setName(event.target.value)} value={name} />
          <small>
            {kindName} · {dirty ? t('unsaved') : t('saved')}
            <span className={`assistant-status flows-status-${flow.status}`}>{t(`status.${flow.status}`)}</span>
            {flow.live_version && <span>{flow.live_version}</span>}
            {flow.has_draft_changes && <em>{t('draftChanges')}</em>}
          </small>
        </div>
        <div className="flows-editor-actions">
          <button className="secondary-button" onClick={() => void toggleChecks()} type="button"><ClipboardCheck size={15} /> {t('checkConfig')}</button>
          {canEdit && <button className="secondary-button" disabled={busy || !dirty || !name.trim()} onClick={() => void onSave()} type="button">{t('saveDraft')}</button>}
          {canPublish && flow.status !== 'disabled' && <button className="primary-button" disabled={busy || !name.trim()} onClick={() => setPublishing(true)} type="button">{flow.live_version ? t('publishUpdate') : t('publish')}</button>}
          {(canEdit || canPublish) && (
            <span className="assistant-menu">
              <button aria-expanded={menuOpen} aria-haspopup="menu" aria-label={t('more')} className="icon-button" onClick={(event) => { event.stopPropagation(); setMenuOpen(!menuOpen) }} type="button"><ChevronDown size={16} /></button>
              {menuOpen && (
                <ul role="menu">
                  {canEdit && <li role="none"><button onClick={() => void duplicate()} role="menuitem" type="button">{t('duplicate')}</button></li>}
                  {canPublish && flow.status === 'disabled' && <li role="none"><button onClick={() => void transition('enable')} role="menuitem" type="button">{t('enable')}</button></li>}
                  {canPublish && flow.status === 'published' && <li role="none"><button className="danger" onClick={() => setConfirm('disable')} role="menuitem" type="button">{t('disable')}</button></li>}
                </ul>
              )}
            </span>
          )}
        </div>
      </header>
      {checksOpen && (
        <div className={`flows-checks ${checks?.ok ? 'ok' : 'warn'}`} role="status">
          {checks?.ok ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
          <span>{checks === null ? t('loading') : checks.ok ? t('checksPassed') : <><strong>{t('checksPending')}</strong> {checks.issues.map((issue) => issue.message).join('；')}</>}</span>
          <button aria-label={t('close')} className="icon-button" onClick={() => setChecksOpen(false)} type="button"><X size={15} /></button>
        </div>
      )}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="flows-editor-body">
        <div className="detail-card">
          <h2>{t('nodesTitle', { count: nodes.length })}</h2>
          <ul className="flows-node-list">
            {nodes.map((node, index) => {
              const invalid = flow.invalid_nodes.includes(node.id)
              return (
                <li className={invalid ? 'invalid' : ''} key={node.id}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{node.name || t(`nodeType.${node.type}`)}</strong>
                  <small>{t(`nodeType.${node.type}`)}</small>
                  {invalid && <em><AlertTriangle size={13} /> {t('invalidTarget')}</em>}
                </li>
              )
            })}
            {nodes.length === 0 && <li className="empty">{t('noNodes')}</li>}
          </ul>
          <ul className="flows-tab-list">{TABS[flow.kind].map((tab) => <li key={tab}>{t(`tab.${tab}`)}</li>)}</ul>
          <p className="flows-pending">{t('bodyPending', { kind: kindName })}</p>
          {flow.reference_count > 0 && <p className="flows-pending">{t('referencedBy', { count: flow.reference_count })}</p>}
        </div>
        <div className="detail-card">
          <h2>{t('versionsTitle')}</h2>
          {versions.length === 0 && <p className="flows-pending">{t('noVersions')}</p>}
          <ul className="flows-version-list">
            {versions.map((version) => (
              <li key={version.id}>
                <strong>{version.version}</strong>
                {version.is_live && <span className="assistant-status published">{t('live')}</span>}
                <span>{version.change_note || t('noNote')}</span>
                <small>{version.published_by ?? ''} · {new Date(version.published_at).toLocaleString()}</small>
              </li>
            ))}
          </ul>
        </div>
      </div>
      {publishing && (
        <div className="modal-backdrop" role="presentation" onClick={() => setPublishing(false)}>
          <div aria-modal="true" className="create-modal flows-publish" onClick={(event) => event.stopPropagation()} role="dialog" aria-label={t('publishTitle')}>
            <h2>{t('publishTitle')}</h2>
            <p>{t('publishBody')}</p>
            <label className="kb-field">
              <span>{t('changeNote')}</span>
              <textarea maxLength={500} onChange={(event) => setNote(event.target.value)} placeholder={t('changeNotePlaceholder')} rows={3} value={note} />
            </label>
            <div className="kb-form-actions">
              <button className="kb-cancel" onClick={() => setPublishing(false)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={busy} onClick={() => void onPublish()} type="button">{t('publishConfirm')}</button>
            </div>
          </div>
        </div>
      )}
      {confirm === 'leave' && (
        <ConfirmDialog body={t('discardBody')} danger onCancel={() => setConfirm(null)} onConfirm={() => navigate(listPath)} title={t('discardTitle')} />
      )}
      {confirm === 'disable' && (
        <ConfirmDialog
          body={flow.reference_count > 0 ? t('disableBodyReferenced', { count: flow.reference_count }) : t('disableBody')}
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={async () => { setConfirm(null); await transition('disable') }}
          title={t('disableTitle', { kind: kindName })}
        />
      )}
    </section>
  )
}
