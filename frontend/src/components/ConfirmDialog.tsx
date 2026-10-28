import { useState } from 'react'
import { useTranslation } from 'react-i18next'

/** Custom confirmation dialog (GLB-044); never use window.confirm. */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel,
  danger = false,
  onConfirm,
  onCancel,
}: {
  title: string
  body: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  onConfirm: () => Promise<void> | void
  onCancel: () => void
}) {
  const { t } = useTranslation('assistants')
  const [working, setWorking] = useState(false)

  async function confirm() {
    setWorking(true)
    try {
      await onConfirm()
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onCancel}>
      <div aria-labelledby="confirm-title" aria-modal="true" className="create-modal confirm-dialog" onClick={(event) => event.stopPropagation()} role="alertdialog">
        <h2 id="confirm-title">{title}</h2>
        <p>{body}</p>
        <div className="confirm-actions">
          <button className="secondary-button" disabled={working} onClick={onCancel} type="button">{cancelLabel ?? t('confirm.cancel')}</button>
          <button className={danger ? 'primary-button danger-button' : 'primary-button'} disabled={working} onClick={() => void confirm()} type="button">
            {working ? t('confirm.working') : confirmLabel ?? t('confirm.confirm')}
          </button>
        </div>
      </div>
    </div>
  )
}
