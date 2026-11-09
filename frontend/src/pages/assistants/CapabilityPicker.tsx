import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

type Candidate = { id: string; name: string; kb_type: 'standard' | 'faq'; documents: number; faqs: number }

/** Capability picker (AST-022 / AST-062). Only knowledge bases can be mounted so far; other types say "coming soon". */
export function CapabilityPicker({
  tenantId,
  selected,
  names,
  onChange,
}: {
  tenantId: string
  selected: string[]
  names: Record<string, string>
  onChange: (ids: string[], names: Record<string, string>) => void
}) {
  const { t } = useTranslation('assistants')
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<Candidate[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ kind: 'kb', limit: '100' })
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
  }, [query, tenantId, t])

  const allSelected = Boolean(items && items.length > 0 && items.every((item) => selected.includes(item.id)))

  function toggle(item: Candidate, checked: boolean) {
    const nextNames = { ...names, [item.id]: item.name }
    onChange(checked ? [...selected, item.id] : selected.filter((id) => id !== item.id), nextNames)
  }

  function toggleAll() {
    if (!items) return
    if (allSelected) {
      onChange(selected.filter((id) => !items.some((item) => item.id === id)), names)
    } else {
      const merged = [...selected, ...items.map((item) => item.id).filter((id) => !selected.includes(id))]
      onChange(merged, { ...names, ...Object.fromEntries(items.map((item) => [item.id, item.name])) })
    }
  }

  return (
    <div className="capability-picker">
      <nav aria-label={t('picker.kinds')} className="capability-kinds">
        <button aria-pressed className="selected" type="button">{t('wizard.capabilities.kb')}<small>{selected.length}</small></button>
        {(['agent', 'mcp', 'skill', 'chatflow', 'flow', 'api'] as const).map((key) => (
          <button disabled key={key} title={t('wizard.capabilities.soon')} type="button">{t(`wizard.capabilities.${key}`)}<small>{t('wizard.capabilities.soon')}</small></button>
        ))}
      </nav>
      <div className="capability-list">
        <div className="capability-toolbar">
          <input aria-label={t('picker.search', { kind: t('wizard.capabilities.kb') })} onChange={(event) => setQuery(event.target.value)} placeholder={t('picker.search', { kind: t('wizard.capabilities.kb') })} value={query} />
          <button className="text-button" disabled={!items || items.length === 0} onClick={toggleAll} type="button">{allSelected ? t('picker.clear') : t('picker.selectAll')}</button>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        {!items && !error && <p className="wizard-muted">…</p>}
        {items && items.length === 0 && <p className="wizard-muted">{query.trim() ? t('picker.noMatch') : t('picker.empty')}</p>}
        <ul>
          {items?.map((item) => (
            <li key={item.id}>
              <label>
                <input checked={selected.includes(item.id)} onChange={(event) => toggle(item, event.target.checked)} type="checkbox" />
                <span>{item.name}</span>
                <em>{t(item.kb_type === 'faq' ? 'picker.typeFaq' : 'picker.typeStandard')}</em>
                <small>{item.kb_type === 'faq' ? t('picker.faqs', { count: item.faqs }) : t('picker.docs', { count: item.documents })}</small>
              </label>
            </li>
          ))}
        </ul>
        <div className="capability-selected" aria-label={t('picker.selected', { count: selected.length })}>
          <strong>{t('picker.selected', { count: selected.length })}</strong>
          {selected.map((id) => (
            <span className="capability-chip" key={id}>
              {names[id] ?? `${id.slice(0, 8)} ${t('picker.unknown')}`}
              <button aria-label={t('picker.remove')} onClick={() => onChange(selected.filter((value) => value !== id), names)} type="button"><X size={12} /></button>
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
