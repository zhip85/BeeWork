import { useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { PiiRuleDialog } from '../../components/pii/PiiRuleDialog'
import { PiiRuleTable } from '../../components/pii/PiiRuleTable'
import { EMPTY_RULE } from '../../components/pii/types'
import type { RuleDraft, RuleLabel, RuleRow } from '../../components/pii/types'
import type { StagePanelProps } from './stagePanels'

type LocalRule = { id: string; from_id: string | null; name: string; match_type: RuleRow['match_type']; pattern: string; stage: RuleRow['stage']; action: RuleRow['action']; block_message: string; enabled: boolean }
type PiiConfig = { enabled: boolean; use_global: boolean; rules: LocalRule[] }
type Item = Omit<RuleRow, 'pending'> & { switched_off: boolean }
type Preview = {
  chain: { platform_total: number; platform_enforced: number; tenant_inherited: number; tenant_level: number; assistant_inherited: number; assistant_level: number; effective: number }
  items: Item[]
}

const readConfig = (oc: Record<string, unknown>): PiiConfig => {
  const pii = (oc.pii ?? {}) as Partial<PiiConfig>
  return { enabled: pii.enabled ?? true, use_global: pii.use_global ?? true, rules: pii.rules ?? [] }
}

/** OC · PII rules (OC-010 / OC-011): the assistant layer of the inheritance on the shared rule table. Edits go to the draft OC. */
export function PiiStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('governance')
  const pii = readConfig(oc)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<RuleDraft | null>(null)
  const [inherited, setInherited] = useState(false)
  const [formError, setFormError] = useState('')
  const [deleting, setDeleting] = useState<RuleRow | null>(null)
  const [scope, setScope] = useState<'all' | 'assistant'>('all')
  const piiKey = JSON.stringify(pii)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      apiRequest<Preview>(`/api/v1/assistants/${assistantId}/oc/pii/preview`, { method: 'POST', body: JSON.stringify({ pii: JSON.parse(piiKey) }) }, tenant.id)
        .then((result) => { setPreview(result); setError('') })
        .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
    }, 250)
    return () => window.clearTimeout(timer)
  }, [assistantId, tenant.id, piiKey, t])

  const update = (next: Partial<PiiConfig>) => onChange({ ...oc, pii: { ...pii, ...next } })
  const setRules = (rules: LocalRule[]) => update({ rules })
  const rows: RuleRow[] = (preview?.items ?? [])
    .filter((item) => scope === 'all' || item.label === 'assistant' || item.label === 'assistant_modified')
    .map((item) => ({ ...item, pending: false, switchedOff: item.switched_off }))
  const chain = preview?.chain

  /** Save the fields of a row on the assistant layer: edit an own rule in place, otherwise create / update the override. */
  function put(key: string, label: RuleLabel, fields: Omit<LocalRule, 'id' | 'from_id'>) {
    if (label === 'assistant') return setRules(pii.rules.map((rule) => (rule.id === key ? { ...rule, ...fields } : rule)))
    const existing = pii.rules.find((rule) => rule.from_id === key)
    if (existing) return setRules(pii.rules.map((rule) => (rule.id === existing.id ? { ...rule, ...fields } : rule)))
    return setRules([...pii.rules, { id: crypto.randomUUID(), from_id: key, ...fields }])
  }
  const fieldsOf = (row: RuleRow, enabled = row.enabled) => ({ name: row.name, match_type: row.match_type, pattern: row.pattern, stage: row.stage, action: row.action, block_message: row.block_message, enabled })

  function open(row: RuleRow | null) {
    setFormError('')
    setInherited(!!row && row.label !== 'assistant' && row.label !== 'assistant_modified')
    setDraft(row ? { key: row.key, name: row.name, match_type: row.match_type, pattern: row.pattern, stage: row.stage, action: row.action, block_message: row.block_message, enabled: row.enabled } : EMPTY_RULE)
  }

  function save() {
    if (!draft) return
    const fields = { name: draft.name.trim(), match_type: draft.match_type, pattern: draft.pattern, stage: draft.stage, action: draft.action, block_message: draft.action === 'block' ? draft.block_message : '', enabled: draft.enabled }
    if (!fields.name) return setFormError(t('field.nameRequired'))
    if (draft.key) {
      const row = preview?.items.find((item) => item.key === draft.key)
      put(draft.key, (row?.label ?? 'assistant') as RuleLabel, fields)
    } else {
      setRules([...pii.rules, { id: crypto.randomUUID(), from_id: null, ...fields }])
    }
    setDraft(null)
  }

  return (
    <div className="governance-stage">
      {error && <p className="members-error" role="alert">{error}</p>}
      {chain && (
        <ol aria-label={t('assistant.chainLabel')} className="governance-chain governance-chain-three">
          <li><strong>{t('assistant.chainPlatform')}</strong><small>{t('assistant.chainPlatformCounts', { total: chain.platform_total, enforced: chain.platform_enforced })}</small></li>
          <li><strong>{t('assistant.chainTenant')}</strong><small>{t('assistant.chainTenantCounts', { inherited: chain.tenant_inherited, own: chain.tenant_level })}</small></li>
          <li><strong>{t('assistant.chainAssistant')}</strong><small>{t('assistant.chainAssistantCounts', { inherited: chain.assistant_inherited, own: chain.assistant_level, effective: chain.effective })}</small></li>
        </ol>
      )}
      <label className="governance-inherit">
        <input checked={pii.enabled} disabled={readOnly} onChange={(event) => update({ enabled: event.target.checked })} type="checkbox" />
        <span><strong>{t('assistant.detection')}</strong><small>{t('assistant.detectionHint')}</small></span>
      </label>
      <label className="governance-inherit">
        <input checked={pii.use_global} disabled={readOnly || !pii.enabled} onChange={(event) => update({ use_global: event.target.checked })} type="checkbox" />
        <span><strong>{t('assistant.useGlobal')}</strong><small>{t('assistant.useGlobalHint')}</small></span>
      </label>
      <div className="knowledge-status-bar">
        {(['all', 'assistant'] as const).map((value) => <button aria-pressed={scope === value} className={scope === value ? 'active' : ''} key={value} onClick={() => setScope(value)} type="button">{t(`assistant.scope.${value}`)}</button>)}
        {!readOnly && <button className="primary-button" onClick={() => open(null)} style={{ marginLeft: 'auto' }} type="button"><Plus size={15} /> {t('assistant.new')}</button>}
      </div>
      <p className="usage-hint">{t('tenant.priority')}</p>
      {rows.length === 0 && <p className="assistants-empty-filtered">{t('tenant.empty')}</p>}
      {rows.length > 0 && (
        <PiiRuleTable
          detectionOff={!pii.enabled} mode="assistant" onDelete={(row) => setDeleting(row)} onEdit={(row) => open(row)}
          onRestore={(row) => setRules(pii.rules.filter((rule) => rule.from_id !== row.key))} onToggle={(row) => put(row.key, row.label, fieldsOf(row, !row.enabled))}
          readOnly={readOnly} rows={rows}
        />
      )}
      {draft && (
        <PiiRuleDialog
          draft={draft} error={formError} inherited={inherited} mode="assistant" onChange={setDraft} onClose={() => setDraft(null)} onSave={save} saving={false}
          titleKey={draft.key ? 'assistant.editTitle' : 'assistant.new'}
        />
      )}
      {deleting && (
        <ConfirmDialog
          body={t('deleteBody', { name: deleting.name })} danger onCancel={() => setDeleting(null)}
          onConfirm={() => { const target = deleting; setDeleting(null); setRules(pii.rules.filter((rule) => rule.id !== target.key)) }} title={t('deleteTitle')}
        />
      )}
    </div>
  )
}
