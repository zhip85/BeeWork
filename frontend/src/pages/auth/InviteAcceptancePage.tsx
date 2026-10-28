import { useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../../i18n/LanguageSwitch'
import { apiRequest } from '../../api'
import type { CurrentUser } from '../../types'

export function InviteAcceptancePage({
  onAccepted,
}: {
  onAccepted: (user: CurrentUser) => void
}) {
  const { t } = useTranslation('auth')
  const location = useLocation()
  const token = new URLSearchParams(location.search).get('token') ?? ''
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  async function acceptInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const user = await apiRequest<CurrentUser>('/api/v1/members/invitations/accept', {
        method: 'POST',
        body: JSON.stringify({
          token,
          password,
          display_name: displayName.trim() || null,
        }),
      })
      onAccepted(user)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('invite.failed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-screen">
      <section className="auth-card">
        <div className="login-language"><LanguageSwitch /></div>
        <div className="eyebrow"><span className="eyebrow-dot" />{t('invite.eyebrow')}</div>
        <h1>{t('invite.title')}</h1>
        <p>{t('invite.body')}</p>
        {!token && <div className="members-error" role="alert">{t('invite.missingToken')}</div>}
        {error && <div className="members-error" role="alert">{error}</div>}
        <form className="login-form" onSubmit={(event) => void acceptInvitation(event)}>
          <label>{t('invite.name')}<input autoComplete="name" maxLength={120} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
          <label>{t('invite.password')}<input autoComplete="new-password" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} /><small>{t('invite.passwordHint')}</small></label>
          <button className="primary-button" disabled={!token || submitting} type="submit">{submitting ? t('invite.submitting') : t('invite.submit')}</button>
        </form>
      </section>
    </main>
  )
}
