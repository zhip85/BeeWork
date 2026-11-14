import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ExperienceEditor } from '../channels/ExperienceEditor'

type ChannelType = { code: string; name: string; version: string; enabled: boolean; connection_params: { label: string }[]; capabilities: string[] }

/** Channel types (PLT-100–102): the platform enables or disables a type and edits its version. A type needs an adapter in code, so none can be added here. */
export function PlatformChannelTypesPage() {
  const { t } = useTranslation('platform')
  const [types, setTypes] = useState<ChannelType[]>([])
  const [versions, setVersions] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    apiRequest<{ items: ChannelType[] }>('/api/v1/platform/channel-types')
      .then((result) => { if (active) { setTypes(result.items); setVersions(Object.fromEntries(result.items.map((item) => [item.code, item.version]))) } })
      .catch((requestError: Error) => active && setError(requestError.message))
    return () => { active = false }
  }, [])

  async function save(code: string, body: { enabled?: boolean; version?: string }) {
    setError('')
    try {
      const updated = await apiRequest<ChannelType>(`/api/v1/platform/channel-types/${code}`, { method: 'PATCH', body: JSON.stringify(body) })
      setTypes((current) => current.map((item) => (item.code === code ? updated : item)))
      setNotice(t('channelTypes.saved'))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('channelTypes.failed'))
    }
  }

  return (
    <section className="settings-page">
      <div className="page-heading"><div><h1>{t('channelTypes.title')}</h1><p>{t('channelTypes.subtitle')}</p></div></div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="channel-type-grid">
        {types.map((item) => (
          <article className={`channel-type-card${item.enabled ? '' : ' unavailable'}`} key={item.code}>
            <header><strong>{item.name}</strong><small>{item.code}</small></header>
            <p>{t('channelTypes.params', { params: item.connection_params.map((param) => param.label).join(' · ') })}</p>
            <p className="channel-tags">{item.capabilities.map((tag) => <span key={tag}>{tag}</span>)}</p>
            <label>{t('channelTypes.version')}
              <input maxLength={16} onChange={(event) => setVersions({ ...versions, [item.code]: event.target.value })} value={versions[item.code] ?? ''} />
            </label>
            <div className="confirm-actions">
              <button className="secondary-button" disabled={!versions[item.code] || versions[item.code] === item.version} onClick={() => void save(item.code, { version: versions[item.code] })} type="button">{t('channelTypes.saveVersion')}</button>
              <button className="secondary-button" onClick={() => setEditing(item.code)} type="button">{t('channelTypes.editDefault')}</button>
              <button className={item.enabled ? 'danger-button' : 'primary-button'} onClick={() => void save(item.code, { enabled: !item.enabled })} type="button">{item.enabled ? t('channelTypes.disable') : t('channelTypes.enable')}</button>
            </div>
            <small className="wizard-muted">{item.enabled ? t('channelTypes.disableHint') : t('channelTypes.disabledHint')}</small>
          </article>
        ))}
      </div>
      {editing && <ExperienceEditor channelType={editing} level="platform" onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice(t('channelTypes.saved')) }} />}
    </section>
  )
}
