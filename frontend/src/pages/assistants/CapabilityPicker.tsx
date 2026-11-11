import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

type Candidate = { id: string; kind: string; name: string; kb_type?: 'standard' | 'faq' | null; documents?: number | null; faqs?: number | null; meta?: Record<string, unknown> }
type TypeEntry = { kind: string; mountable: boolean; available: number }
export type Selection = Record<string, string[]>

const KIND_ORDER = ['kb', 'agent', 'mcp', 'skill', 'chatflow', 'flow', 'api'] as const

/** Capability picker (AST-022 / AST-062): every capability type with a badge of what is selected, search, select all /
 * clear, candidate details and a summary of the selection. Types nobody can mount yet say "coming soon". */
export function CapabilityPicker({
  tenantId,
  selected,
  names,
  hiddenKinds = [],
  onChange,
}: {
  tenantId: string
  hiddenKinds?: string[]
  selected: Selection
  names: Record<string, string>
  onChange: (selected: Selection, names: Record<string, string>) => void
}) {
  const { t } = useTranslation('assistants')
  const [types, setTypes] = useState<TypeEntry[] | null>(null)
  const [kind, setKind] = useState<string>('kb')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<Candidate[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    apiRequest<{ items: TypeEntry[] }>('/api/v1/capabilities/types', {}, tenantId)
      .then((result) => setTypes(result.items))
      .catch((requestError: Error) => setError(requestError.message || t('picker.loadFailed')))
  }, [tenantId, t])

  const mountable = (key: string) => types?.find((entry) => entry.kind === key)?.mountable ?? key === 'kb'

  useEffect(() => {
    if (!mountable(kind)) return
    let active = true
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ kind, limit: '100' })
      if (query.trim()) params.set('q', query.trim())
      apiRequest<{ items: Candidate[] }>(`/api/v1/capabilities/candidates?${params}`, {}, tenantId)
        .then((result) => {
          if (!active) return
          setItems(result.items)
          setError('')
        })
        .catch((requestError: Error) => active && setError(requestError.message || t('picker.loadFailed')))
    }, 200)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, query, tenantId, types, t])

  const chosen = selected[kind] ?? []
  const kindName = t(`wizard.capabilities.${kind}`)
  const allSelected = Boolean(items && items.length > 0 && items.every((item) => chosen.includes(item.id)))
  const total = Object.values(selected).reduce((sum, ids) => sum + ids.length, 0)

  function setKindIds(ids: string[], nextNames: Record<string, string>) {
    onChange({ ...selected, [kind]: ids }, nextNames)
  }

  function toggle(item: Candidate, checked: boolean) {
    setKindIds(checked ? [...chosen, item.id] : chosen.filter((id) => id !== item.id), { ...names, [item.id]: item.name })
  }

  function toggleAll() {
    if (!items) return
    if (allSelected) {
      setKindIds(chosen.filter((id) => !items.some((item) => item.id === id)), names)
    } else {
      setKindIds([...chosen, ...items.map((item) => item.id).filter((id) => !chosen.includes(id))], { ...names, ...Object.fromEntries(items.map((item) => [item.id, item.name])) })
    }
  }

  function describe(item: Candidate): string {
    if (item.kind === 'kb') return item.kb_type === 'faq' ? t('picker.faqs', { count: item.faqs ?? 0 }) : t('picker.docs', { count: item.documents ?? 0 })
    const summary = item.meta?.summary
    return typeof summary === 'string' ? summary : ''
  }

  return (
    <div className="capability-picker">
      <nav aria-label={t('picker.kinds')} className="capability-kinds">
        {KIND_ORDER.filter((key) => !hiddenKinds.includes(key)).map((key) => (
          <button
            aria-pressed={kind === key}
            className={kind === key ? 'selected' : ''}
            disabled={!mountable(key)}
            key={key}
            onClick={() => { setKind(key); setQuery(''); setItems(null) }}
            title={mountable(key) ? undefined : t('wizard.capabilities.soon')}
            type="button"
          >
            {t(`wizard.capabilities.${key}`)}
            <small>{mountable(key) ? (selected[key]?.length ?? 0) : t('wizard.capabilities.soon')}</small>
          </button>
        ))}
      </nav>
      <div className="capability-list">
        <div className="capability-toolbar">
          <input aria-label={t('picker.search', { kind: kindName })} onChange={(event) => setQuery(event.target.value)} placeholder={t('picker.search', { kind: kindName })} value={query} />
          <button className="text-button" disabled={!items || items.length === 0} onClick={toggleAll} type="button">{allSelected ? t('picker.clear') : t('picker.selectAll')}</button>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        {!items && !error && <p className="wizard-muted">…</p>}
        {items && items.length === 0 && <p className="wizard-muted">{query.trim() ? t('picker.noMatch') : t(`picker.empty.${kind}`, { defaultValue: t('picker.emptyGeneric') })}</p>}
        <ul>
          {items?.map((item) => (
            <li key={item.id}>
              <label>
                <input checked={chosen.includes(item.id)} onChange={(event) => toggle(item, event.target.checked)} type="checkbox" />
                <span>{item.name}</span>
                {item.kind === 'kb' && <em>{t(item.kb_type === 'faq' ? 'picker.typeFaq' : 'picker.typeStandard')}</em>}
                <small>{describe(item)}</small>
              </label>
            </li>
          ))}
        </ul>
        <div className="capability-selected" aria-label={t('picker.selected', { count: total })}>
          <strong>{t('picker.selected', { count: total })}</strong>
          {Object.entries(selected).flatMap(([key, ids]) => ids.map((id) => (
            <span className="capability-chip" key={`${key}:${id}`}>
              {names[id] ?? `${id.slice(0, 8)} ${t('picker.unknown')}`}
              <button aria-label={t('picker.remove')} onClick={() => onChange({ ...selected, [key]: ids.filter((value) => value !== id) }, names)} type="button"><X size={12} /></button>
            </span>
          )))}
        </div>
      </div>
    </div>
  )
}
