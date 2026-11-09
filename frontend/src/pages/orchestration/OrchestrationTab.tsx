import { useEffect, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { OcState } from './types'

/** The “编排” tab of the assistant detail page (PRD 3.2.6): the request pipeline as six steps, each linking to its OC stage. */
export function OrchestrationTab({ assistantId, tenant }: { assistantId: string; tenant: TenantMembership }) {
  const { t } = useTranslation('orchestration')
  const allowed = tenant.permissions.includes('orchestration:view')
  const [state, setState] = useState<OcState | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!allowed) return
    apiRequest<OcState>(`/api/v1/assistants/${assistantId}/oc`, {}, tenant.id)
      .then((result) => { setState(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [allowed, assistantId, tenant.id, t])

  if (!allowed) return <div className="detail-card"><p className="assistants-empty-filtered">{t('denied')}</p></div>
  return (
    <div className="detail-card orchestration-tab">
      <div className="detail-card-head">
        <div><h2>{t('pipelineTitle')}</h2><p className="usage-hint">{t('pipelineSubtitle')}</p></div>
        <Link className="primary-button" to={`/assistants/${assistantId}/oc`}>{t('configureOc')}</Link>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {state && (
        <>
          <ol className="orchestration-steps">
            {state.stages.map((row) => (
              <li key={row.stage}>
                <Link to={`/assistants/${assistantId}/oc/${row.stage}`}>
                  <small>{row.index}</small>
                  <strong>{t(`stages.${row.stage}.name`)}</strong>
                  <span>{row.available ? row.summary : t('soon')}</span>
                  <ChevronRight size={14} />
                </Link>
              </li>
            ))}
          </ol>
          <p className="usage-hint">
            {state.pending_approval_id ? t('pending') : state.has_changes ? t('draftDiffers') : t('inSync')}
          </p>
        </>
      )}
    </div>
  )
}
