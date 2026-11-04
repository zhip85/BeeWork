import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../api'
import type { TenantEnvironmentName } from '../types'

type Item = { environment: TenantEnvironmentName; available: boolean; gateway_sync_status: string }

const POLL_MS = 60000

/** Top-bar environment switch (GLB-021): plan-limited choices and a health dot from the gateway sync state. */
export function EnvironmentSwitcher({
  tenantId,
  value,
  onChange,
}: {
  tenantId: string
  value: TenantEnvironmentName
  onChange: (environment: TenantEnvironmentName) => void
}) {
  const { t } = useTranslation('common')
  const [items, setItems] = useState<Item[]>([])

  useEffect(() => {
    let active = true
    const load = () => apiRequest<{ environments: Item[] }>('/api/v1/tenant/environments', {}, tenantId, true)
      .then((result) => active && setItems(result.environments))
      .catch(() => active && setItems([]))
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => { active = false; window.clearInterval(timer) }
  }, [tenantId, value])

  const current = items.find((item) => item.environment === value)
  const offered = items.filter((item) => item.available)
  const health = current?.gateway_sync_status === 'synced' ? 'ok' : current?.gateway_sync_status === 'failed' ? 'bad' : 'idle'

  return (
    <label className="environment-switch" title={t(`environment.health.${health}`)}>
      <i className={`env-dot ${health}`} aria-hidden="true" />
      <span className="visually-hidden">{t('environment.label')}</span>
      {offered.length > 1 ? (
        <select aria-label={t('environment.label')} onChange={(event) => onChange(event.target.value as TenantEnvironmentName)} value={value}>
          {offered.map((item) => <option key={item.environment} value={item.environment}>{t(`environment.names.${item.environment}`)}</option>)}
        </select>
      ) : (
        <span className="environment-fixed" title={t('environment.productionOnly')}>{t(`environment.names.${value}`)}</span>
      )}
    </label>
  )
}
