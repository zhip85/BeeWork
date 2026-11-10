import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

export function SettingsTabs() {
  const { t } = useTranslation('settings')
  return (
    <nav aria-label="settings" className="settings-tabs">
      <NavLink end to="/settings">{t('tabs.models')}</NavLink>
      <NavLink to="/settings/privacy">{t('tabs.privacy')}</NavLink>
      <NavLink to="/settings/rules">{t('tabs.rules')}</NavLink>
      <NavLink to="/settings/glossary">{t('tabs.glossary')}</NavLink>
      <NavLink to="/settings/approvals">{t('tabs.approvals')}</NavLink>
      <NavLink to="/settings/usage">{t('tabs.usage')}</NavLink>
      <NavLink to="/settings/api">{t('tabs.api')}</NavLink>
    </nav>
  )
}
