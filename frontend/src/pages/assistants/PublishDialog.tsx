import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { ApiRequestError, apiRequest } from '../../api'
import { ChangeList } from './VersionDiff'
import type { VersionChange } from './VersionDiff'
import type { Assistant } from './types'

type Preview = {
  next_version: string
  blockers: { code: string; message: string }[]
  warnings: { code: string; severity: 'high' | 'info'; message: string }[]
  has_changes: boolean
  diff: VersionChange[]
  can_publish: boolean
  approval: { required: boolean; levels: number; approver_role: string; pending_approval_id: string | null }
}

export function PublishDialog({
  assistant,
  tenantId,
  onClose,
  onPublished,
  onSubmitted,
}: {
  assistant: Assistant
  tenantId: string
  onClose: () => void
  onPublished: (version: string) => void
  onSubmitted: (version: string) => void
}) {
  const { t } = useTranslation('versions')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [confirmReplace, setConfirmReplace] = useState(false)

  useEffect(() => {
    let active = true
    apiRequest<Preview>(`/api/v1/assistants/${assistant.id}/publish-preview`, {}, tenantId)
      .then((result) => active && setPreview(result))
      .catch((requestError: Error) => active && setError(requestError.message || t('publish.loadFailed')))
    return () => { active = false }
  }, [assistant.id, tenantId, t])

  async function submit(replacePending = false) {
    if (!preview) return
    setSubmitting(true)
    setError('')
    try {
      const result = await apiRequest<{ pending_approval?: boolean }>(`/api/v1/assistants/${assistant.id}/publish`, {
        method: 'POST', body: JSON.stringify({ change_note: note, replace_pending: replacePending }),
      }, tenantId)
      if (result.pending_approval) onSubmitted(preview.next_version)
      else onPublished(preview.next_version)
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'approval.pending_exists') setConfirmReplace(true)
      else setError(requestError instanceof Error ? requestError.message : t('publish.loadFailed'))
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <div aria-labelledby="publish-title" aria-modal="true" className="create-modal publish-dialog" onClick={(event) => event.stopPropagation()} role="dialog">
        <button aria-label={t('actions.close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <h2 id="publish-title">{t('publish.title')} · {assistant.name}</h2>
        {!preview && !error && <p>{t('publish.loading')}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {preview && (
          <>
            <p>{t('publish.subtitle', { version: preview.next_version })}</p>
            {preview.blockers.length > 0
              ? <div className="publish-blockers" role="alert"><strong>{t('publish.blockers')}</strong><ul>{preview.blockers.map((b) => <li key={b.code}>{b.message}</li>)}</ul></div>
              : <p className="assistants-notice">{t('publish.noBlockers')}</p>}
            {preview.warnings.length > 0 && <div className="publish-warnings"><strong>{t('publish.warnings')}</strong><ul>{preview.warnings.map((w) => <li key={w.code + w.message}>{w.message}</li>)}</ul></div>}
            {!preview.has_changes && <p className="wizard-muted">{t('publish.noChanges')}</p>}
            {preview.has_changes && assistant.live_version_id === null && <p className="wizard-muted">{t('publish.firstRelease')}</p>}
            {preview.diff.length > 0 && <><h3>{t('publish.changes')}</h3><ChangeList changes={preview.diff} /></>}
            <label className="publish-note">{t('publish.note')}
              <textarea maxLength={500} onChange={(event) => setNote(event.target.value)} placeholder={t('publish.notePlaceholder')} rows={3} value={note} />
            </label>
            {preview.approval.pending_approval_id && <p className="publish-warnings">{t('publish.pendingExists')} <Link onClick={onClose} to={`/approvals?id=${preview.approval.pending_approval_id}`}>{t('publish.viewApproval')}</Link></p>}
            {confirmReplace && <p className="publish-warnings" role="alert">{t('publish.replaceBody')} <button className="text-button" disabled={submitting} onClick={() => { setConfirmReplace(false); void submit(true) }} type="button">{t('publish.replaceConfirm')}</button></p>}
            <p className="wizard-muted">{preview.approval.required ? t('publish.approvalRequired', { levels: preview.approval.levels, role: t(`publish.roles.${preview.approval.approver_role}`) }) : t('publish.approvalNote')}</p>
            <div className="confirm-actions">
              <button className="secondary-button" disabled={submitting} onClick={onClose} type="button">{t('publish.cancel')}</button>
              <button className="primary-button" disabled={submitting || !preview.can_publish} onClick={() => void submit()} type="button">
                {submitting ? t('publish.submitting') : t(preview.approval.required ? 'publish.submitForApproval' : 'publish.submit', { version: preview.next_version })}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
