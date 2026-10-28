import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import enAudit from './en/audit.json'
import enAuth from './en/auth.json'
import enCommon from './en/common.json'
import enEnums from './en/enums.json'
import enMembers from './en/members.json'
import enNav from './en/nav.json'
import enPlatform from './en/platform.json'
import enSettings from './en/settings.json'
import zhAudit from './zh-CN/audit.json'
import zhAuth from './zh-CN/auth.json'
import zhCommon from './zh-CN/common.json'
import zhEnums from './zh-CN/enums.json'
import zhMembers from './zh-CN/members.json'
import zhNav from './zh-CN/nav.json'
import zhPlatform from './zh-CN/platform.json'
import zhSettings from './zh-CN/settings.json'

export const LOCALES = ['zh-CN', 'en'] as const
export type Locale = (typeof LOCALES)[number]
export const DEFAULT_LOCALE: Locale = 'zh-CN'
const STORAGE_KEY = 'beework.locale'

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && (LOCALES as readonly string[]).includes(value)
}

function storedLocale(): Locale {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    if (isLocale(value)) return value
  } catch {
    // localStorage can be unavailable (private mode); fall back to the default.
  }
  return DEFAULT_LOCALE
}

void i18n.use(initReactI18next).init({
  lng: storedLocale(),
  fallbackLng: DEFAULT_LOCALE,
  defaultNS: 'common',
  ns: ['common', 'nav', 'auth', 'members', 'platform', 'settings', 'audit', 'enums'],
  resources: {
    'zh-CN': {
      common: zhCommon, nav: zhNav, auth: zhAuth, members: zhMembers,
      platform: zhPlatform, settings: zhSettings, audit: zhAudit, enums: zhEnums,
    },
    en: {
      common: enCommon, nav: enNav, auth: enAuth, members: enMembers,
      platform: enPlatform, settings: enSettings, audit: enAudit, enums: enEnums,
    },
  },
  interpolation: { escapeValue: false },
})

document.documentElement.lang = i18n.language
i18n.on('languageChanged', (lng) => {
  document.documentElement.lang = lng
})

export function currentLocale(): Locale {
  return isLocale(i18n.language) ? i18n.language : DEFAULT_LOCALE
}

/** Apply a locale in the UI and remember it in this browser. */
export function applyLocale(locale: Locale) {
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    // Preference still applies for this page view.
  }
  return i18n.changeLanguage(locale)
}

export type EnumGroup =
  | 'plan' | 'region' | 'modelRegion' | 'modelTypeLong' | 'modelType'
  | 'modelProvider' | 'modelAvailability' | 'tenantStatus' | 'gatewaySync' | 'role'

/** Label for a backend enum value; unknown values are shown as-is. */
export function enumLabel(group: EnumGroup, value: string) {
  return i18n.t(`${group}.${value}`, { ns: 'enums', defaultValue: value })
}

/** All `[value, label]` options of an enum group, in dictionary order. */
export function enumOptions(group: EnumGroup): [string, string][] {
  const keys = Object.keys(
    (i18n.getResourceBundle(currentLocale(), 'enums') as Record<string, Record<string, string>>)[group] ?? {},
  )
  return keys.map((key) => [key, enumLabel(group, key)])
}

export default i18n
