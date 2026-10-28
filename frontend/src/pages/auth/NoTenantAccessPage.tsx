import {
  Building2,
  LogOut,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { LanguageSwitch } from '../../i18n/LanguageSwitch'
import type { CurrentUser } from '../../types'

export function NoTenantAccessPage({
  user,
  onLogout,
}: {
  user: CurrentUser
  onLogout: () => Promise<void>
}) {
  const { t } = useTranslation()
  return (
    <main className="no-tenant-screen">
      <section className="no-tenant-card">
        <div className="login-language"><LanguageSwitch persist /></div>
        <span className="platform-identity-icon"><Building2 size={19} /></span>
        <div className="section-kicker">{t('auth:noTenant.kicker')}</div>
        <h1>{t('auth:noTenant.title')}</h1>
        <p>{t('auth:noTenant.body', { email: user.email })}</p>
        <button className="primary-button" onClick={() => void onLogout()} type="button"><LogOut size={15} />{t('logout')}</button>
      </section>
    </main>
  )
}
