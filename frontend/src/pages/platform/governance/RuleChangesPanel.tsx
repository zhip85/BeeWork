import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../../api'
import type { ChangeRequest } from './types'

/** Pending high-risk rule changes (PLT-071): another administrator approves or rejects; the requester only waits. */
export function RuleChangesPanel({ requests, currentUserId, onDecided, onError }: {
  requests: ChangeRequest[]
  currentUserId: string
  onDecided: (message: string) => void
  onError: (message: string) => void
}) {
  const { t } = useTranslation('governance')
  const [busy, setBusy] = useState('')
  const [rejecting, setRejecting] = useState<ChangeRequest | null>(null)
  const [comment, setComment] = useState('')

  async function decide(request: ChangeRequest, action: 'approve' | 'reject', text = '') {
    setBusy(request.id)
    try {
      await apiRequest(`/api/v1/platform/rule-changes/${request.id}/${action}`, { method: 'POST', body: JSON.stringify({ comment: text }) })
      setRejecting(null)
      setComment('')
      onDecided(t(action === 'approve' ? 'changes.approved' : 'changes.rejected', { name: request.object_name }))
    } catch (requestError) {
      onError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setBusy('')
    }
  }

  if (requests.length === 0) return null
  return (
    <div className="panel governance-changes">
      <div className="members-table-heading">
        <div><div className="section-kicker">{t('changes.kicker')}</div><h2>{t('changes.title')}</h2></div>
        <span className="tenant-role-chip">{t('changes.hint')}</span>
      </div>
      <ul className="governance-change-list">
        {requests.map((request) => (
          <li key={request.id}>
            <div>
              <strong>{request.summary}</strong>
              <small>{t('changes.by', { name: request.requested_by_name })}{request.reason ? ` · ${request.reason}` : ''} · {t('changes.expires', { time: new Date(request.expires_at).toLocaleString() })}</small>
            </div>
            {request.requested_by_id === currentUserId
              ? <span className="role-text">{t('changes.waiting')}</span>
              : (
                <div className="plans-actions">
                  <button className="tenant-status-action" disabled={busy === request.id} onClick={() => void decide(request, 'approve')} type="button">{t('changes.approve')}</button>
                  <button className="tenant-status-action suspend" disabled={busy === request.id} onClick={() => { setRejecting(request); setComment('') }} type="button">{t('changes.reject')}</button>
                </div>
              )}
          </li>
        ))}
      </ul>
      {rejecting && (
        <div className="modal-backdrop" onClick={() => setRejecting(null)} role="presentation">
          <form className="create-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void decide(rejecting, 'reject', comment) }} role="dialog" aria-modal="true">
            <h2>{t('changes.rejectTitle')}</h2>
            <p>{rejecting.summary}</p>
            <label>{t('changes.rejectReason')}<textarea maxLength={500} onChange={(event) => setComment(event.target.value)} required rows={3} value={comment} /></label>
            <div className="plans-actions">
              <button className="text-button" onClick={() => setRejecting(null)} type="button">{t('cancel')}</button>
              <button className="primary-button danger-button" disabled={!comment.trim() || busy === rejecting.id} type="submit">{t('changes.reject')}</button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
