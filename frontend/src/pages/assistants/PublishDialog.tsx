import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ChangeList } from './VersionDiff'
import type { VersionChange } from './VersionDiff'
import type { Assistant } from './types'

type Preview = {
  next_version: string
  blockers: { code: string; message: string }[]
  has_changes: boolean
  diff: VersionChange[]
  can_publish: boolean
}

export function PublishDialog({
  assistant,
  tenantId,
  onClose,
  onPublished,
}: {
  assistant: Assistant
  tenantId: string
  onClose: () => void
  onPublished: (version: string) => void
}) {
  const { t } = useTranslation('versions')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    let active = true
    apiRequest<Preview>(`/api/v1/assistants/${assistant.id}/publish-preview`, {}, tenantId)
      .then((result) => active && setPreview(result))
      .catch((requestError: Error) => active && setError(requestError.message || t('publish.loadFailed')))
    return () => { active = false }
  }, [assistant.id, tenantId, t])

  async function submit() {
    if (!preview) return
    setSubmitting(true)
    setError('')
    try {
      await apiRequest(`/api/v1/assistants/${assistant.id}/publish`, { method: 'POST', body: JSON.stringify({ change_note: note }) }, tenantId)
      onPublished(preview.next_version)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('publish.loadFailed'))
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
            {!preview.has_changes && <p className="wizard-muted">{t('publish.noChanges')}</p>}
            {preview.has_changes && assistant.live_version_id === null && <p className="wizard-muted">{t('publish.firstRelease')}</p>}
            {preview.diff.length > 0 && <><h3>{t('publish.changes')}</h3><ChangeList changes={preview.diff} /></>}
            <label className="publish-note">{t('publish.note')}
              <textarea maxLength={500} onChange={(event) => setNote(event.target.value)} placeholder={t('publish.notePlaceholder')} rows={3} value={note} />
            </label>
            <p className="wizard-muted">{t('publish.approvalNote')}</p>
            <div className="confirm-actions">
              <button className="secondary-button" disabled={submitting} onClick={onClose} type="button">{t('publish.cancel')}</button>
              <button className="primary-button" disabled={submitting || !preview.can_publish} onClick={() => void submit()} type="button">
                {submitting ? t('publish.submitting') : t('publish.submit', { version: preview.next_version })}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
