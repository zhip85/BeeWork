import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ACTIONS, MATCH_TYPES } from './types'
import type { MatchType, RuleAction, RuleDraft, RuleMode, RuleStage } from './types'

const KICKER: Record<RuleMode, string> = { platform: 'PLATFORM', tenant: 'TENANT', assistant: 'ASSISTANT LEVEL' }

/** The rule dialog shared by the three layers (OC-011): common fields plus layer-specific extras. */
export function PiiRuleDialog({ mode, draft, inherited, saving, error, extras, titleKey, onChange, onSave, onClose }: {
  mode: RuleMode
  draft: RuleDraft
  inherited: boolean
  saving: boolean
  error: string
  extras?: ReactNode
  titleKey: string
  onChange: (next: RuleDraft) => void
  onSave: () => void
  onClose: () => void
}) {
  const { t } = useTranslation('governance')
  const needsPattern = draft.match_type === 'regex' || draft.match_type === 'keyword'
  return (
    <div className="modal-backdrop" onClick={() => !saving && onClose()} role="presentation">
      <form aria-labelledby="pii-rule-title" aria-modal="true" className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); onSave() }} role="dialog">
        <div className="section-kicker">{KICKER[mode]}</div>
        <h2 id="pii-rule-title">{t(titleKey)}</h2>
        {inherited && <p className="assistants-notice">{t(`inheritedNotice.${mode}`)}</p>}
        <label>{t('field.name')}<input autoFocus maxLength={80} onChange={(event) => onChange({ ...draft, name: event.target.value })} required value={draft.name} /></label>
        <label>{t('field.matchType')}
          <select onChange={(event) => onChange({ ...draft, match_type: event.target.value as MatchType })} value={draft.match_type}>
            {MATCH_TYPES.map((value) => <option key={value} value={value}>{t(`matchType.${value}`)}</option>)}
          </select>
        </label>
        <label>{t('field.stage')}
          <select onChange={(event) => onChange({ ...draft, stage: event.target.value as RuleStage })} value={draft.stage}>
            {(['both', 'input', 'output'] as const).map((value) => <option key={value} value={value}>{t(`stage.${value}`)}</option>)}
          </select>
        </label>
        <label>{t('field.pattern')}
          <textarea className="governance-mono" onChange={(event) => onChange({ ...draft, pattern: event.target.value })} required={needsPattern} rows={3} value={draft.pattern} />
          <small>{t(`patternHint.${draft.match_type}`)}</small>
        </label>
        <label>{t('field.action')}
          <select onChange={(event) => onChange({ ...draft, action: event.target.value as RuleAction })} value={draft.action}>
            {ACTIONS.map((value) => <option key={value} value={value}>{t(`action.${value}`)}</option>)}
          </select>
        </label>
        {draft.action === 'block' && (
          <label>{t('field.blockMessage')}<textarea maxLength={300} onChange={(event) => onChange({ ...draft, block_message: event.target.value })} placeholder={t('field.blockMessagePlaceholder')} rows={2} value={draft.block_message} /></label>
        )}
        {extras}
        <label className="governance-check"><input checked={draft.enabled} onChange={(event) => onChange({ ...draft, enabled: event.target.checked })} type="checkbox" /> {t('field.enabled')}</label>
        {error && <p className="members-error" role="alert">{error}</p>}
        <div className="plans-actions">
          <button className="text-button" disabled={saving} onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={saving || !draft.name.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
        </div>
      </form>
    </div>
  )
}
