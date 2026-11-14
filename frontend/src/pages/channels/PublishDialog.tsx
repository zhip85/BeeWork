import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { Channel } from './ChannelDialog'

type Preview = {
  version_from: string
  version_to: string
  first_publish: boolean
  changes: { field: string; from: unknown; to: unknown }[]
  problems: string[]
  components: { name: string }[]
  components_note: string
  injection_prompt: string | null
  trial: { name: string }[]
  trial_note: string
}

const show = (value: unknown) => (value === null || value === undefined || value === '' ? '—' : Array.isArray(value) ? value.join(', ') : String(value))

/** The publish dialog (CHN-020–024): what changes against the live version, what blocks publishing, then confirm. */
export function PublishDialog({ channel, tenantId, onCancel, onConfirm }: { channel: Channel; tenantId: string; onCancel: () => void; onConfirm: () => Promise<void> }) {
  const { t } = useTranslation('channels')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [tab, setTab] = useState<'components' | 'prompt' | 'trial'>('components')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true
    apiRequest<Preview>(`/api/v1/channels/${channel.id}/publish-preview`, {}, tenantId)
      .then((result) => active && setPreview(result))
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [channel.id, tenantId])

  async function confirm() {
    setBusy(true)
    await onConfirm()
    setBusy(false)
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onCancel}>
      <div aria-modal="true" className="create-modal channel-publish" onClick={(event) => event.stopPropagation()} role="dialog" aria-label={t('publish.dialogTitle')}>
        <button aria-label={t('close')} className="modal-close" onClick={onCancel} type="button"><X size={18} /></button>
        <small>{t('publish.dialogTitle')}</small>
        <h2>{channel.name} · {preview?.version_to ?? ''}</h2>
        <p className="wizard-muted">{t('publish.snapshotNote')}</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        {!preview && !error && <p className="wizard-muted">{t('loading')}</p>}
        {preview && (
          <>
            {preview.problems.length > 0 && (
              <ul className="form-error" role="alert">{preview.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>
            )}
            <div className="channel-publish-tabs" role="tablist">
              {(['components', 'prompt', 'trial'] as const).map((value) => (
                <button aria-selected={tab === value} className={tab === value ? 'active' : ''} key={value} onClick={() => setTab(value)} role="tab" type="button">{t(`publish.tabs.${value}`)}</button>
              ))}
            </div>
            {tab === 'components' && (
              <div role="tabpanel">
                <p className="wizard-muted">{preview.first_publish ? t('publish.first') : t('publish.against', { version: preview.version_from })}</p>
                {preview.changes.length > 0 && (
                  <ul className="channel-diff">{preview.changes.map((change) => <li key={change.field}><strong>{change.field}</strong> <del>{show(change.from)}</del> → <ins>{show(change.to)}</ins></li>)}</ul>
                )}
                <p className="wizard-muted">{preview.components.length ? preview.components.map((c) => c.name).join(' · ') : preview.components_note}</p>
              </div>
            )}
            {tab === 'prompt' && <div role="tabpanel"><p className="wizard-muted">{preview.injection_prompt ?? t('publish.noPrompt')}</p></div>}
            {tab === 'trial' && <div role="tabpanel"><p className="wizard-muted">{preview.trial.length ? preview.trial.map((c) => c.name).join(' · ') : preview.trial_note}</p></div>}
          </>
        )}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onCancel} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={busy || !preview || preview.problems.length > 0} onClick={() => void confirm()} type="button">{busy ? t('saving') : t('publish.confirm')}</button>
        </div>
      </div>
    </div>
  )
}
