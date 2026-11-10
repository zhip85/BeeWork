import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Check, ChevronDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiRequestError, apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { TenantMembership } from '../../types'
import { ChangeList } from '../assistants/VersionDiff'
import { AssistantNav } from '../assistants/AssistantNav'
import { STAGE_PANELS } from './stagePanels'
import { STAGES } from './types'
import type { OcAssistantOption, OcConfig, OcState, Stage } from './types'

const OVERVIEW = 'overview'

/** Orchestration control (PRD 3.6.1–3.6.2): stage navigation, assistant selector, save draft and submit for approval. */
export function OrchestrationPage({ tenant }: { tenant: TenantMembership }) {
  const { assistantId = '' } = useParams()
  return <OrchestrationView key={assistantId} tenant={tenant} />  // a fresh state per assistant
}

function OrchestrationView({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('orchestration')
  const { assistantId = '', stage: stageParam } = useParams()
  const navigate = useNavigate()
  const stage: Stage | typeof OVERVIEW = stageParam === OVERVIEW ? OVERVIEW : STAGES.includes(stageParam as Stage) ? (stageParam as Stage) : 'pii'
  const [state, setState] = useState<OcState | null>(null)
  const [draft, setDraft] = useState<OcConfig | null>(null)
  const [options, setOptions] = useState<OcAssistantOption[]>([])
  const [menu, setMenu] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)
  const [leaving, setLeaving] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const permitted = (permission: string) => tenant.access_mode === 'full' && tenant.permissions.includes(permission)
  const canEdit = permitted('orchestration:edit') && (state?.assistant.can_edit ?? false)
  const canSubmit = permitted('orchestration:publish') && (state?.assistant.can_edit ?? false)
  const dirty = state !== null && draft !== null && JSON.stringify(draft) !== JSON.stringify(state.oc)

  const load = useCallback(() => {
    apiRequest<OcState>(`/api/v1/assistants/${assistantId}/oc`, {}, tenant.id)
      .then((result) => { setState(result); setDraft(result.oc); setError('') })
      .catch((requestError: Error) => { setState(null); setError(requestError.message || t('loadFailed')) })
  }, [assistantId, tenant.id, t])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    apiRequest<{ items: OcAssistantOption[] }>('/api/v1/oc/assistants', {}, tenant.id).then((result) => setOptions(result.items)).catch(() => setOptions([]))
  }, [tenant.id])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  /** OC-003 / OC-005: leaving with unsaved changes asks first. */
  function go(path: string) {
    if (dirty) setLeaving(path)
    else navigate(path)
  }

  async function save() {
    if (!state || !draft) return
    setSaving(true)
    setError('')
    try {
      const result = await apiRequest<OcState>(`/api/v1/assistants/${assistantId}/oc`, { method: 'PUT', body: JSON.stringify({ oc: draft, row_version: state.row_version }) }, tenant.id)
      setState(result)
      setDraft(result.oc)
      setNotice(t('saved'))
    } catch (requestError) {
      setError(requestError instanceof ApiRequestError && requestError.status === 409 ? t('stale') : requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  const header = state?.assistant
  const panelProps = { assistantId, tenant, oc: draft ?? {}, readOnly: !canEdit, onChange: setDraft }
  const Panel = stage !== OVERVIEW ? STAGE_PANELS[stage] : undefined

  return (
    <section className="detail-page orchestration-page">
      <button className="back-link" onClick={() => go(`/assistants/${assistantId}`)} type="button"><ArrowLeft size={14} /> {t('back')}</button>
      <header className="page-heading">
        <div>
          <p className="section-kicker">{header?.name ?? '…'}</p>
          <h1>{t('title')}</h1>
          <p>{t('subtitle')}</p>
        </div>
        <div className="plans-actions">
          {canEdit && <button className="secondary-button" disabled={!dirty || saving} onClick={() => void save()} type="button">{saving ? t('saving') : t('save')}</button>}
          {canSubmit && <button className="primary-button" disabled={dirty || saving || !state} onClick={() => setSubmitting(true)} title={dirty ? t('saveFirst') : undefined} type="button">{t('submit')}</button>}
        </div>
      </header>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {state?.pending_approval_id && <p className="assistants-notice">{t('pending')} <Link to="/approvals">{t('viewApproval')}</Link></p>}
      {dirty && <p className="usage-hint">{t('unsaved')}</p>}

      <div className="orchestration-layout">
        <div className="orchestration-side">
          <div className="orchestration-selector">
            <button aria-expanded={menu} onClick={() => setMenu(!menu)} type="button">
              <span>{t('assistant')} · {header?.name ?? '…'}</span><ChevronDown size={14} />
            </button>
            {menu && (
              <ul role="listbox">
                {options.map((option) => (
                  <li key={option.id}>
                    <button
                      onClick={() => { setMenu(false); if (option.id !== assistantId) go(`/assistants/${option.id}/oc/${stage}`) }}
                      role="option" aria-selected={option.id === assistantId} type="button"
                    >
                      <strong>{option.name}</strong>
                      <small>{option.version ? `v${option.version}` : t('notPublished')} · {t(`status.${option.status}`, { defaultValue: option.status })}</small>
                      {option.id === assistantId && <Check size={14} />}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <AssistantNav active={`oc:${stage}`} assistantId={assistantId} go={go} showOc />
        </div>

        <div className="orchestration-content">
          {!state && !error && <p>{t('loading')}</p>}
          {state && stage === OVERVIEW && (
            <div className="detail-card">
              <h2>{t('overview')}</h2>
              <table className="assistants-table">
                <tbody>
                  {state.stages.map((row) => (
                    <tr key={row.stage}>
                      <td><strong>{row.index}. {t(`stages.${row.stage}.name`)}</strong></td>
                      <td>{row.available ? row.summary : <span className="usage-hint">{t('soon')}</span>}</td>
                      <td><button className="text-button" onClick={() => go(`/assistants/${assistantId}/oc/${row.stage}`)} type="button">{t('configure')}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {state && stage !== OVERVIEW && (
            <div className="detail-card">
              <h2>{t(`stages.${stage}.name`)}</h2>
              {Panel ? <Panel {...panelProps} /> : <p className="assistants-empty-filtered">{t('soonStage', { name: t(`stages.${stage}.name`) })}</p>}
            </div>
          )}
        </div>
      </div>

      {leaving && (
        <ConfirmDialog
          body={t('leaveBody')} cancelLabel={t('stay')} confirmLabel={t('leave')} danger onCancel={() => setLeaving(null)}
          onConfirm={() => { const target = leaving; setLeaving(null); setDraft(state?.oc ?? null); navigate(target) }} title={t('leaveTitle')}
        />
      )}
      {submitting && state && (
        <SubmitDialog
          onClose={() => setSubmitting(false)}
          onDone={(message) => { setSubmitting(false); setNotice(message); load() }}
          state={state} tenantId={tenant.id}
        />
      )}
    </section>
  )
}

function SubmitDialog({ state, tenantId, onClose, onDone }: { state: OcState; tenantId: string; onClose: () => void; onDone: (message: string) => void }) {
  const { t } = useTranslation('orchestration')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmReplace, setConfirmReplace] = useState(false)
  const neverPublished = state.assistant.live_version === null

  async function submit(replacePending = false) {
    setBusy(true)
    setError('')
    try {
      const result = await apiRequest<{ pending_approval?: boolean }>(`/api/v1/assistants/${state.assistant.id}/oc/submit`, {
        method: 'POST', body: JSON.stringify({ change_note: note, replace_pending: replacePending }),
      }, tenantId)
      onDone(result.pending_approval ? t('submitted') : t('applied'))
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'approval.pending_exists') setConfirmReplace(true)
      else setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" onClick={onClose} role="presentation">
      <div aria-labelledby="oc-submit-title" aria-modal="true" className="create-modal publish-dialog" onClick={(event) => event.stopPropagation()} role="dialog">
        <h2 id="oc-submit-title">{t('submitTitle', { name: state.assistant.name })}</h2>
        {neverPublished && <p className="form-error" role="alert">{t('publishFirst')}</p>}
        {!neverPublished && !state.has_changes && <p className="wizard-muted">{t('noChanges')}</p>}
        {state.has_changes && <ChangeList changes={state.diff} />}
        <p className="usage-hint">{state.approval.required ? t('approvalRequired') : t('approvalOff')}</p>
        <label>{t('note')}<textarea maxLength={500} onChange={(event) => setNote(event.target.value)} rows={3} value={note} /></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="plans-actions">
          <button className="text-button" disabled={busy} onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={busy || neverPublished || !state.has_changes} onClick={() => void submit()} type="button">{busy ? t('submitting') : t('submit')}</button>
        </div>
        {confirmReplace && (
          <ConfirmDialog body={t('replaceBody')} onCancel={() => setConfirmReplace(false)} onConfirm={() => { setConfirmReplace(false); return submit(true) }} title={t('replaceTitle')} />
        )}
      </div>
    </div>
  )
}
