import { useEffect, useState } from 'react'
import { Pencil, Plus, RotateCcw, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import type { StagePanelProps } from './stagePanels'

type LocalTerm = { id: string; from_id: string | null; hidden: boolean; term: string; aliases: string[]; definition: string }
type GlossaryConfig = { use_tenant: boolean; terms: LocalTerm[] }
type Item = { key: string; term: string; aliases: string[]; definition: string; label: string; restorable: boolean; deletable: boolean }
type Preview = {
  chain: { industries: string[]; industry_terms: number; tenant_total: number; tenant_level: number; assistant_inherited: number; assistant_level: number; hidden: number; effective: number }
  items: Item[]
  hidden: Item[]
}
type Draft = { key: string | null; label: string; term: string; aliases: string; definition: string }

const readConfig = (oc: Record<string, unknown>): GlossaryConfig => {
  const glossary = (oc.glossary ?? {}) as Partial<GlossaryConfig>
  return { use_tenant: glossary.use_tenant ?? true, terms: glossary.terms ?? [] }
}
const splitAliases = (value: string) => value.split(/[,，、;；\n]/).map((item) => item.trim()).filter(Boolean)

/** OC · glossary (OC-030 – OC-037): the assistant layer on top of the inherited platform and tenant terms. Edits go to the draft OC. */
export function GlossaryStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('orchestration')
  const glossary = readConfig(oc)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [deleting, setDeleting] = useState<Item | null>(null)
  const [scope, setScope] = useState<'all' | 'assistant'>('all')
  const key = JSON.stringify(glossary)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      apiRequest<Preview>(`/api/v1/assistants/${assistantId}/oc/glossary/preview`, { method: 'POST', body: JSON.stringify({ glossary: JSON.parse(key) }) }, tenant.id)
        .then((result) => { setPreview(result); setError('') })
        .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
    }, 250)
    return () => window.clearTimeout(timer)
  }, [assistantId, tenant.id, key, t])

  const update = (next: Partial<GlossaryConfig>) => onChange({ ...oc, glossary: { ...glossary, ...next } })
  const setTerms = (terms: LocalTerm[]) => update({ terms })
  const rows = (preview?.items ?? []).filter((item) => scope === 'all' || item.label.startsWith('assistant'))
  const chain = preview?.chain

  function open(item: Item | null) {
    setDraft(item ? { key: item.key, label: item.label, term: item.term, aliases: item.aliases.join('、'), definition: item.definition } : { key: null, label: 'assistant', term: '', aliases: '', definition: '' })
  }

  function save() {
    if (!draft || !draft.term.trim()) return
    const fields = { hidden: false, term: draft.term.trim(), aliases: splitAliases(draft.aliases), definition: draft.definition.trim() }
    if (!draft.key) setTerms([...glossary.terms, { id: crypto.randomUUID(), from_id: null, ...fields }])
    else if (draft.label === 'assistant') setTerms(glossary.terms.map((item) => (item.id === draft.key ? { ...item, ...fields } : item)))
    else {
      const existing = glossary.terms.find((item) => item.from_id === draft.key)
      setTerms(existing ? glossary.terms.map((item) => (item.id === existing.id ? { ...item, ...fields } : item)) : [...glossary.terms, { id: crypto.randomUUID(), from_id: draft.key, ...fields }])
    }
    setDraft(null)
  }

  function remove(item: Item) {
    if (item.label === 'assistant') return setTerms(glossary.terms.filter((entry) => entry.id !== item.key))
    const rest = glossary.terms.filter((entry) => entry.from_id !== item.key)
    setTerms([...rest, { id: crypto.randomUUID(), from_id: item.key, hidden: true, term: item.term, aliases: [], definition: '' }])
  }

  return (
    <div className="oc-glossary-panel">
      <p className="usage-hint">{t('glossary.hint')}</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {chain && (
        <ol aria-label={t('glossary.chainLabel')} className="governance-chain governance-chain-three">
          <li><strong>{t('glossary.chainIndustry')}</strong><small>{t('glossary.chainIndustryCounts', { names: chain.industries.length ? chain.industries.join('、') : t('glossary.none'), count: chain.industry_terms })}</small></li>
          <li><strong>{t('glossary.chainTenant')}</strong><small>{t('glossary.chainTenantCounts', { total: chain.tenant_total, own: chain.tenant_level })}</small></li>
          <li><strong>{t('glossary.chainAssistant')}</strong><small>{t('glossary.chainAssistantCounts', { inherited: chain.assistant_inherited, own: chain.assistant_level, effective: chain.effective })}</small></li>
        </ol>
      )}
      <label className="governance-inherit">
        <input checked={glossary.use_tenant} disabled={readOnly} onChange={(event) => update({ use_tenant: event.target.checked })} type="checkbox" />
        <span><strong>{t('glossary.useTenant')}</strong><small>{t('glossary.useTenantHint')}</small></span>
      </label>
      <div className="knowledge-status-bar">
        {(['all', 'assistant'] as const).map((value) => <button aria-pressed={scope === value} className={scope === value ? 'active' : ''} key={value} onClick={() => setScope(value)} type="button">{t(`glossary.scope.${value}`)}</button>)}
      </div>
      {!readOnly && <div className="plans-actions"><button className="primary-button" onClick={() => open(null)} type="button"><Plus size={15} /> {t('glossary.new')}</button></div>}
      {chain && chain.hidden > 0 && (
        <p className="assistants-notice">
          {t('glossary.hiddenCount', { count: chain.hidden })}{' '}
          {!readOnly && <button className="text-button" onClick={() => setTerms(glossary.terms.filter((entry) => !entry.hidden))} type="button">{t('glossary.restoreAll')}</button>}
        </p>
      )}
      {rows.length === 0 && <p className="assistants-empty-filtered">{t('glossary.empty')}</p>}
      {rows.length > 0 && (
        <div className="members-table-wrap">
          <table className="members-table">
            <thead><tr><th>{t('glossary.col.term')}</th><th>{t('glossary.col.aliases')}</th><th>{t('glossary.col.definition')}</th><th>{t('glossary.col.level')}</th><th /></tr></thead>
            <tbody>
              {rows.map((item) => (
                <tr key={item.key}>
                  <td><strong>{item.term}</strong></td>
                  <td>{item.aliases.length ? item.aliases.join('、') : '—'}</td>
                  <td className="glossary-definition">{item.definition || '—'}</td>
                  <td><span className={`assistant-status ${item.label.startsWith('assistant') ? 'published' : 'draft'}`}>{t(`glossary.layer.${item.label}`)}</span></td>
                  <td className="member-actions-cell">
                    {!readOnly && <button aria-label={t('glossary.edit')} className="icon-button" onClick={() => open(item)} title={t('glossary.edit')} type="button"><Pencil size={15} /></button>}
                    {!readOnly && item.restorable && <button aria-label={t('glossary.restore')} className="icon-button" onClick={() => setTerms(glossary.terms.filter((entry) => entry.from_id !== item.key))} title={t('glossary.restore')} type="button"><RotateCcw size={15} /></button>}
                    {!readOnly && <button aria-label={t('glossary.delete')} className="icon-button" onClick={() => setDeleting(item)} title={t('glossary.delete')} type="button"><Trash2 size={15} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <div className="modal-backdrop" onClick={() => setDraft(null)} role="presentation">
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); save() }} role="dialog" aria-modal="true">
            <div className="section-kicker">ASSISTANT</div>
            <h2>{t(draft.key ? 'glossary.editTitle' : 'glossary.new')}</h2>
            {draft.key && draft.label !== 'assistant' && <p className="assistants-notice">{t('glossary.inheritedNotice')}</p>}
            <label>{t('glossary.col.term')} *<input autoFocus maxLength={200} onChange={(event) => setDraft({ ...draft, term: event.target.value })} required value={draft.term} /></label>
            <label>{t('glossary.col.aliases')}<input onChange={(event) => setDraft({ ...draft, aliases: event.target.value })} value={draft.aliases} /><small>{t('glossary.aliasesHint')}</small></label>
            <label>{t('glossary.col.definition')}<textarea maxLength={1000} onChange={(event) => setDraft({ ...draft, definition: event.target.value })} rows={3} value={draft.definition} /></label>
            <div className="plans-actions">
              <button className="text-button" onClick={() => setDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={!draft.term.trim()} type="submit">{t('glossary.confirm')}</button>
            </div>
          </form>
        </div>
      )}
      {deleting && (
        <ConfirmDialog
          body={t(deleting.label === 'assistant' ? 'glossary.deleteOwnBody' : 'glossary.hideBody', { name: deleting.term })} danger onCancel={() => setDeleting(null)}
          onConfirm={() => { const target = deleting; setDeleting(null); remove(target) }} title={t('glossary.deleteTitle')}
        />
      )}
    </div>
  )
}
