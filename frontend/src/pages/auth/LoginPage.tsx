import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  ArrowRight,
  Hexagon,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../../i18n/LanguageSwitch'

export function LoginPage({
  onLogin,
  error,
}: {
  onLogin: (email: string, password: string) => Promise<void>
  error: string
}) {
  const { t } = useTranslation('auth')
  const [loginType, setLoginType] = useState<'tenant' | 'platform'>('tenant')
  const [email, setEmail] = useState(import.meta.env.DEV ? 'admin@beework.dev' : '')
  const [password, setPassword] = useState(import.meta.env.DEV ? 'Beework-Dev-2026!' : '')
  const [formError, setFormError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setFormError('')
    try {
      await onLogin(email, password)
    } catch (loginError) {
      setFormError(loginError instanceof Error ? loginError.message : t('login.failed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-screen">
      <section className="login-panel">
        <div className="login-language"><LanguageSwitch /></div>
        <Link className="brand login-brand" to="/" aria-label="BeeWork">
          <span className="brand-mark"><Hexagon size={22} strokeWidth={2.2} /><span /></span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">TAP</span>
        </Link>
        <div className="login-eyebrow"><span className="eyebrow-dot" />{loginType === 'platform' ? t('login.eyebrowPlatform') : t('login.eyebrowTenant')}</div>
        <h1>{(loginType === 'platform' ? t('login.titlePlatform') : t('login.titleTenant')).split('\n').map((line, index) => <span key={line}>{index > 0 && <br />}{line}</span>)}</h1>
        <p className="login-description">{loginType === 'platform' ? t('login.descPlatform') : t('login.descTenant')}</p>
        <div className="login-type-switch" role="group" aria-label={t('login.entryLabel')}>
          <button
            aria-pressed={loginType === 'tenant'}
            className={loginType === 'tenant' ? 'selected' : ''}
            onClick={() => {
              setLoginType('tenant')
              if (import.meta.env.DEV) {
                setEmail('admin@beework.dev')
                setPassword('Beework-Dev-2026!')
              }
            }}
            type="button"
          >{t('login.tenantTab')}</button>
          <button
            aria-pressed={loginType === 'platform'}
            className={loginType === 'platform' ? 'selected' : ''}
            onClick={() => {
              setLoginType('platform')
              if (import.meta.env.DEV) {
                setEmail('platform@beework.dev')
                setPassword('BeeWork-Platform-2026!')
              }
            }}
            type="button"
          >{t('login.platformTab')}</button>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label htmlFor="login-email">{loginType === 'platform' ? t('login.emailPlatform') : t('login.emailTenant')}</label>
          <input
            autoComplete="username"
            id="login-email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
          <label htmlFor="login-password">{t('login.password')}</label>
          <input
            autoComplete="current-password"
            id="login-password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          {(formError || error) && <p className="login-error" role="alert">{formError || error}</p>}
          <button className="primary-button login-submit" disabled={submitting} type="submit">
            {submitting ? t('login.submitting') : loginType === 'platform' ? t('login.submitPlatform') : t('login.submitTenant')} <ArrowRight size={16} />
          </button>
        </form>
        {import.meta.env.DEV && (
          <div className="login-demo-note">
            <strong>{loginType === 'platform' ? t('login.demoPlatform') : t('login.demoTenant')}</strong>
            <span>{loginType === 'platform' ? 'platform@beework.dev' : 'admin@beework.dev'}</span>
            <span>{loginType === 'platform' ? 'BeeWork-Platform-2026!' : 'Beework-Dev-2026!'}</span>
          </div>
        )}
        <span className="login-footnote">{t('login.footnote')}</span>
      </section>
      <aside className="login-aside">
        <div className="login-aside-orbit orbit-one" />
        <div className="login-aside-orbit orbit-two" />
        <div className="login-aside-copy">
          <div className="login-aside-kicker">BUILD · RUN · GOVERN</div>
          <h2>{t('login.asideLead').split('\n').map((line) => <span key={line}>{line}<br /></span>)}<em>{t('login.asideEm')}</em></h2>
          <p>{t('login.asideBody')}</p>
          <div className="login-aside-bottom"><span>{t('login.asideBuild')}</span><i /><span>{t('login.asideRun')}</span><i /><span>{t('login.asideGovern')}</span></div>
        </div>
      </aside>
    </main>
  )
}
