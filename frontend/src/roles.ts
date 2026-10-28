import { enumLabel } from './i18n'

export function roleLabel(role: string, customRoleName?: string | null) {
  if (role === 'custom' && customRoleName) return customRoleName
  return enumLabel('role', role)
}
