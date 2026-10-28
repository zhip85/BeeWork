import { useEffect, useState } from 'react'
import { Lock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { enumLabel } from '../../i18n'
import { SettingsTabs } from './SettingsTabs'

type Policy = {
  retention_days: number
  redact_pii: boolean
  allow_export: boolean
  row_version: number
  retention_choices: number[]
  max_retention_days: number | null
}

export function DataPrivacyPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const { t } = useTranslation('settings')
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [draft, setDraft] = useState<Policy | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)
  const [reload, setReload] = useState(0)
  const [region, setRegion] = useState<string | undefined>()

  useEffect(() => {
    let active = true
    apiRequest<{ region: string }>('/api/v1/tenant/model-environments/production', {}, tenantId)
      .then((result) => active && setRegion(enumLabel('region', result.region)))
      .catch(() => active && setRegion(undefined)) // needs models:view; the region is informational
    return () => { active = false }
  }, [tenantId])

  useEffect(() => {
    let active = true
    apiRequest<Policy>('/api/v1/tenant/compliance', {}, tenantId)
      .then((result) => {
        if (!active) return
        setPolicy(result)
        setDraft(result)
      })
      .catch((requestError: Error) => active && setError(requestError.message || t('privacy.loadFailed')))
    return () => { active = false }
  }, [tenantId, reload, t])

  async function save() {
    if (!draft) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const saved = await apiRequest<Policy>(
        '/api/v1/tenant/compliance',
        {
          method: 'PUT',
          body: JSON.stringify({
            retention_days: draft.retention_days,
            redact_pii: draft.redact_pii,
            allow_export: draft.allow_export,
            row_version: draft.row_version,
          }),
        },
        tenantId,
      )
      setPolicy(saved)
      setDraft(saved)
      setNotice(t('privacy.saved'))
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : t('privacy.loadFailed')
      setError(message)
      if (/修改|stale|刷新/.test(message)) {
        setNotice(t('privacy.conflict'))
        setReload((value) => value + 1)
      }
    } finally {
      setSaving(false)
    }
  }

  const dirty = policy && draft && (
    policy.retention_days !== draft.retention_days
    || policy.redact_pii !== draft.redact_pii
    || policy.allow_export !== draft.allow_export
  )

  return (
    <section className="settings-page privacy-page">
      <div className="page-heading"><div><p className="section-kicker">{t('privacy.title')}</p><h1>{t('privacy.title')}</h1><p>{t('privacy.subtitle')}</p></div></div>
      <SettingsTabs />
      {!draft && !error && <p>{t('privacy.loading')}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {draft && (
        <div className="privacy-form">
          <label>
            <span>{t('privacy.retention')}</span>
            <select disabled={!canEdit} onChange={(event) => setDraft({ ...draft, retention_days: Number(event.target.value) })} value={draft.retention_days}>
              {draft.retention_choices.map((days) => <option key={days} value={days}>{t('privacy.retentionDays', { days })}</option>)}
            </select>
            <small>{draft.max_retention_days ? t('privacy.retentionHint', { cap: draft.max_retention_days }) : t('privacy.retentionHintCustom')}</small>
          </label>
          <div className="privacy-row">
            <span>{t('privacy.region')}</span>
            <strong><Lock size={13} /> {region ?? '—'}</strong>
            <small>{t('privacy.regionHint')}</small>
          </div>
          <label className="privacy-toggle">
            <input checked={draft.redact_pii} disabled={!canEdit} onChange={(event) => setDraft({ ...draft, redact_pii: event.target.checked })} type="checkbox" />
            <span><strong>{t('privacy.redact')}</strong><small>{t('privacy.redactHint')}</small></span>
          </label>
          <label className="privacy-toggle">
            <input checked={draft.allow_export} disabled={!canEdit} onChange={(event) => setDraft({ ...draft, allow_export: event.target.checked })} type="checkbox" />
            <span><strong>{t('privacy.export')}</strong><small>{t('privacy.exportHint')}</small></span>
          </label>
          {canEdit
            ? <button className="primary-button" disabled={saving || !dirty} onClick={() => void save()} type="button">{saving ? t('privacy.saving') : t('privacy.save')}</button>
            : <p className="wizard-muted">{t('privacy.readOnly')}</p>}
        </div>
      )}
    </section>
  )
}
