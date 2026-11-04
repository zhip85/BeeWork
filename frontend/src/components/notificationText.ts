import type { TFunction } from 'i18next'
import { enumLabel } from '../i18n'

export type NotificationItem = {
  id: string
  type: string
  title: string
  body: string
  params: Record<string, string>
  link: string | null
  read_at: string | null
  created_at: string
}

/** Known types render in the viewer's language; the server-written Chinese text is the fallback. */
export function notificationTitle(t: TFunction<'notifications'>, item: NotificationItem) {
  const params = { ...item.params }
  if (params.status) params.status = enumLabel('tenantStatus', params.status)
  return t(`types.${item.type}`, { ...params, defaultValue: item.title })
}

/** Links are in-app paths only; anything else is ignored rather than followed. */
export function safeInAppLink(link: string | null) {
  return link && link.startsWith('/') && !link.startsWith('//') && !link.includes('\\') ? link : null
}
