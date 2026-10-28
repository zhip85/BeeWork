import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

export function SettingsTabs() {
  const { t } = useTranslation('settings')
  return (
    <nav aria-label="settings" className="settings-tabs">
      <NavLink end to="/settings">{t('tabs.models')}</NavLink>
      <NavLink to="/settings/privacy">{t('tabs.privacy')}</NavLink>
    </nav>
  )
}
