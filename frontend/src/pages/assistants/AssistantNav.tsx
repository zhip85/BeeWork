import { useTranslation } from 'react-i18next'
import { STAGES } from '../orchestration/types'

export type NavKey = 'overview' | 'basics' | 'capabilities' | `oc:${string}` | 'channels' | 'tests' | 'versions' | 'conversations' | 'dependencies'

/** Where each entry of the assistant navigation goes. In-page sections use ``?tab=``; the rest are their own routes. */
export function navPath(assistantId: string, key: NavKey): string {
  const base = `/assistants/${assistantId}`
  if (key === 'overview') return base
  if (key.startsWith('oc:')) return `${base}/oc/${key.slice(3)}`
  if (key === 'versions' || key === 'conversations') return `${base}/${key}`
  return `${base}?tab=${key}`
}

/**
 * The one navigation of an assistant, in the order a assistant goes live: basics → capabilities → orchestration →
 * channels → tests → publish. Used by the assistant page and the OC page so every setting is one click away.
 */
export function AssistantNav({
  assistantId, active, go, showOc, capabilityCount,
}: {
  assistantId: string
  active: NavKey
  go: (path: string) => void
  showOc: boolean
  capabilityCount?: number
}) {
  const { t } = useTranslation('detail')
  const { t: to } = useTranslation('orchestration')
  const item = (key: NavKey, label: string, hint?: string) => (
    <button aria-current={active === key ? 'page' : undefined} className={active === key ? 'active' : ''} key={key} onClick={() => go(navPath(assistantId, key))} type="button">
      <strong>{label}</strong>
      {hint && <small>{hint}</small>}
    </button>
  )
  return (
    <nav aria-label={t('navLabel')} className="orchestration-nav assistant-nav">
      {item('overview', t('nav.overview'))}
      {item('basics', t('nav.basics'), t('nav.basicsHint'))}
      {item('capabilities', `${t('nav.capabilities')}${capabilityCount ? ` · ${capabilityCount}` : ''}`, t('nav.capabilitiesHint'))}
      {showOc && !active.startsWith('oc:') && item('oc:overview', t('nav.oc'), t('nav.ocHint'))}
      {showOc && active.startsWith('oc:') && (
        <div>
          <small className="orchestration-group">{t('nav.oc')}</small>
          {item('oc:overview', to('overview'))}
          {STAGES.map((stage) => item(`oc:${stage}`, `${STAGES.indexOf(stage) + 1}. ${to(`stages.${stage}.name`)}`, to(`stages.${stage}.subtitle`)))}
        </div>
      )}
      {item('channels', t('nav.channels'), t('nav.channelsHint'))}
      {item('tests', t('nav.tests'), t('nav.testsHint'))}
      {item('versions', t('nav.versions'), t('nav.versionsHint'))}
      <div>
        <small className="orchestration-group">{t('nav.more')}</small>
        {item('conversations', t('nav.conversations'))}
        {item('dependencies', t('nav.dependencies'))}
      </div>
    </nav>
  )
}
