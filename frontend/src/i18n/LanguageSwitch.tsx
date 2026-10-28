import { useTranslation } from 'react-i18next'
import { apiRequest } from '../api'
import { applyLocale, currentLocale } from './index'
import type { Locale } from './index'

/** EN / 中 toggle. When `persist` is set the choice is also saved to the user profile. */
export function LanguageSwitch({ persist = false }: { persist?: boolean }) {
  const { t } = useTranslation()
  const active = currentLocale()

  async function choose(locale: Locale) {
    if (locale === active) return
    await applyLocale(locale)
    if (!persist) return
    try {
      await apiRequest('/api/v1/auth/me/preferences', {
        method: 'PATCH',
        body: JSON.stringify({ locale }),
      })
    } catch {
      // The browser keeps the choice; the profile syncs on the next successful switch.
    }
  }

  return (
    <div aria-label={t('switchLanguage')} className="language-switch" role="group">
      {([['en', 'EN'], ['zh-CN', '中']] as [Locale, string][]).map(([locale, label]) => (
        <button
          aria-pressed={active === locale}
          className={active === locale ? 'selected' : ''}
          key={locale}
          onClick={() => void choose(locale)}
          type="button"
        >{label}</button>
      ))}
    </div>
  )
}
