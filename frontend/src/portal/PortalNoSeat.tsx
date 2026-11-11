import { useState } from 'react'
import { Hexagon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../i18n/LanguageSwitch'

/** Identity is verified, but this member holds no portal seat (演进 PRD 5.3): ask for one, or switch account.
 * `requested` shows the waiting page. The server decides access on every call; this page only reflects it. */
export default function PortalNoSeat({ name, requested, onRequest, onRefresh, onLogout }: {
  name: string
  requested: boolean
  onRequest: (reason: string) => Promise<void>
  onRefresh: () => void
  onLogout: () => void
}) {
  const { t } = useTranslation('portalApp')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await onRequest(reason)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('noSeat.failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="portal portal-login">
      <div className="portal-login-lang"><LanguageSwitch /></div>
      <section className="portal-login-card portal-noseat">
        <span className="portal-mark"><Hexagon size={22} strokeWidth={2.2} /></span>
        {requested ? (
          <>
            <h1>{t('noSeat.requestedTitle')}</h1>
            <p>{t('noSeat.requestedBody', { name })}</p>
            <button className="portal-primary" onClick={onRefresh} type="button">{t('noSeat.refresh')}</button>
          </>
        ) : (
          <>
            <h1>{t('noSeat.title', { name })}</h1>
            <p>{t('noSeat.body')}</p>
            <label>
              <span>{t('noSeat.reason')}</span>
              <textarea maxLength={200} onChange={(event) => setReason(event.target.value)} placeholder={t('noSeat.reasonHint')} rows={3} value={reason} />
              <small className="portal-hint">{reason.length} / 200</small>
            </label>
            {error && <div className="portal-error" role="alert">{error}</div>}
            <button className="portal-primary" disabled={busy} onClick={() => void submit()} type="button">{t('noSeat.request')}</button>
          </>
        )}
        <button className="portal-link portal-linkbutton" onClick={onLogout} type="button">{t('noSeat.otherAccount')}</button>
      </section>
    </main>
  )
}
