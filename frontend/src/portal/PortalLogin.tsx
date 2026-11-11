import { useState } from 'react'
import type { FormEvent } from 'react'
import { Hexagon } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../i18n/LanguageSwitch'
import './portal.css'

/** Employee sign-in. Email + password only until the identity-provider options of T-146 exist. */
export default function PortalLogin({ onLogin, error }: { onLogin: (email: string, password: string) => Promise<void>; error: string }) {
  const { t } = useTranslation('portalApp')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [formError, setFormError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setFormError('')
    try {
      await onLogin(email, password)
    } catch (failure) {
      setFormError(failure instanceof Error ? failure.message : t('login.failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="portal portal-login">
      <div className="portal-login-lang"><LanguageSwitch /></div>
      <form className="portal-login-card" onSubmit={submit}>
        <span className="portal-mark"><Hexagon size={22} strokeWidth={2.2} /></span>
        <h1>{t('login.title', { name: t('defaultName') })}</h1>
        <p>{t('login.subtitle')}</p>
        <label>
          <span>{t('login.email')}</span>
          <input autoComplete="username" onChange={(event) => setEmail(event.target.value)} required type="email" value={email} />
        </label>
        <label>
          <span>{t('login.password')}</span>
          <input autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
        </label>
        {(formError || error) && <div className="portal-error" role="alert">{formError || error}</div>}
        <button className="portal-primary" disabled={busy} type="submit">{busy ? t('login.submitting') : t('login.submit')}</button>
        <Link className="portal-link" to="/">{t('login.consoleLink')}</Link>
      </form>
    </main>
  )
}
