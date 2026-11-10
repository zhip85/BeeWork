import { useCallback, useEffect, useState } from 'react'
import { BookText, Pencil, Plus, Search, Settings2, Trash2, Upload } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ApiRequestError, apiRequest } from '../../../api'
import { ConfirmDialog } from '../../../components/ConfirmDialog'
import { TermImportDialog } from '../../../components/glossary/TermImportDialog'
import { IndustryManager } from './IndustryManager'
import type { Glossary, Industry, Term, TermList } from './types'

type TermDraft = { id: string | null; term: string; aliases: string; definition: string; row_version: number | null }
type GlossaryDraft = { id: string | null; name: string; description: string; industry_code: string; row_version: number | null }

/** Industry glossaries (PLT-120–126): platform glossaries on the left, the terms of the selected one on the right. */
export function PlatformGlossaryPage() {
  const { t } = useTranslation('glossary')
  const [glossaries, setGlossaries] = useState<Glossary[]>([])
  const [industries, setIndustries] = useState<Industry[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [terms, setTerms] = useState<TermList | null>(null)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [termDraft, setTermDraft] = useState<TermDraft | null>(null)
  const [glossaryDraft, setGlossaryDraft] = useState<GlossaryDraft | null>(null)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [deletingTerm, setDeletingTerm] = useState<Term | null>(null)
  const [deletingGlossary, setDeletingGlossary] = useState<Glossary | null>(null)
  const [importing, setImporting] = useState(false)
  const [managing, setManaging] = useState(false)

  const selected = glossaries.find((item) => item.id === selectedId) ?? null

  const loadGlossaries = useCallback((preferId?: string) => {
    Promise.all([apiRequest<Glossary[]>('/api/v1/platform/glossaries'), apiRequest<Industry[]>('/api/v1/platform/industries')])
      .then(([list, dictionary]) => {
        setGlossaries(list)
        setIndustries(dictionary)
        setSelectedId((current) => (preferId && list.some((g) => g.id === preferId) ? preferId : list.some((g) => g.id === current) ? current : list[0]?.id ?? ''))
        setError('')
      })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [t])
  useEffect(() => { loadGlossaries() }, [loadGlossaries])

  const loadTerms = useCallback(() => {
    if (!selectedId) { setTerms(null); return }
    const params = new URLSearchParams({ limit: '200' })
    if (search.trim()) params.set('q', search.trim())
    apiRequest<TermList>(`/api/v1/platform/glossaries/${selectedId}/terms?${params}`)
      .then((result) => { setTerms(result); setError('') })
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
  }, [selectedId, search, t])
  useEffect(() => {
    const timer = window.setTimeout(loadTerms, 200)
    return () => window.clearTimeout(timer)
  }, [loadTerms])

  async function saveTerm() {
    if (!termDraft || !selected) return
    setSaving(true)
    setFormError('')
    try {
      const body = {
        term: termDraft.term, aliases: termDraft.aliases.split(/[,，;；]/).map((item) => item.trim()).filter(Boolean),
        definition: termDraft.definition, row_version: termDraft.row_version,
      }
      const url = `/api/v1/platform/glossaries/${selected.id}/terms${termDraft.id ? `/${termDraft.id}` : ''}`
      await apiRequest(url, { method: termDraft.id ? 'PUT' : 'POST', body: JSON.stringify(body) })
      setNotice(t('termSaved'))
      setTermDraft(null)
      loadTerms()
      loadGlossaries()
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function saveGlossary() {
    if (!glossaryDraft) return
    setSaving(true)
    setFormError('')
    try {
      const body = { name: glossaryDraft.name, description: glossaryDraft.description, industry_code: glossaryDraft.industry_code, row_version: glossaryDraft.row_version }
      const saved = await apiRequest<Glossary>(glossaryDraft.id ? `/api/v1/platform/glossaries/${glossaryDraft.id}` : '/api/v1/platform/glossaries', { method: glossaryDraft.id ? 'PUT' : 'POST', body: JSON.stringify(body) })
      setNotice(t(glossaryDraft.id ? 'glossarySaved' : 'glossaryCreated'))
      setGlossaryDraft(null)
      loadGlossaries(saved.id)
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setSaving(false)
    }
  }

  async function removeTerm(term: Term) {
    try {
      await apiRequest(`/api/v1/platform/glossaries/${term.glossary_id}/terms/${term.id}`, { method: 'DELETE' })
      setNotice(t('termDeleted'))
      loadTerms()
      loadGlossaries()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  async function removeGlossary(glossary: Glossary, confirm: boolean) {
    try {
      await apiRequest(`/api/v1/platform/glossaries/${glossary.id}?confirm=${confirm}`, { method: 'DELETE' })
      setNotice(t('glossaryDeleted'))
      loadGlossaries()
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.code === 'glossary.in_use') setDeletingGlossary({ ...glossary, tenant_count: Math.max(glossary.tenant_count, 1) })
      else setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    }
  }

  const activeIndustries = industries.filter((item) => item.status === 'active')
  return (
    <section className="platform-governance-page platform-glossary-page">
      <div className="page-heading">
        <div>
          <p className="section-kicker">PLATFORM</p>
          <h1>{t('title')}</h1>
          <p>{t('subtitle')}</p>
        </div>
        <div className="plans-actions">
          <button className="text-button" onClick={() => setManaging(true)} type="button"><Settings2 size={14} /> {t('manageIndustries')}</button>
          <button className="primary-button" onClick={() => { setFormError(''); setGlossaryDraft({ id: null, name: '', description: '', industry_code: activeIndustries[0]?.code ?? '', row_version: null }) }} type="button"><Plus size={15} /> {t('newGlossary')}</button>
        </div>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}

      <div className="glossary-layout">
        <nav aria-label={t('listLabel')} className="glossary-list">
          {glossaries.length === 0 && <p className="assistants-empty-filtered">{t('noGlossary')}</p>}
          {glossaries.map((glossary) => (
            <button aria-current={glossary.id === selectedId ? 'true' : undefined} className={glossary.id === selectedId ? 'active' : ''} key={glossary.id} onClick={() => { setSelectedId(glossary.id); setSearch('') }} type="button">
              <span className="kb-icon violet"><BookText size={16} /></span>
              <span><strong>{glossary.name}</strong><small>{glossary.industry_name} · {t('termCount', { count: glossary.term_count })}</small></span>
            </button>
          ))}
        </nav>

        <div className="panel glossary-terms">
          {!selected && <p className="assistants-empty-filtered">{t('selectGlossary')}</p>}
          {selected && (
            <>
              <div className="detail-card-head">
                <div>
                  <h2>{selected.name} <span className="usage-hint">{terms?.total ?? selected.term_count}</span></h2>
                  <p className="usage-hint">{selected.description ? `${selected.description} · ` : ''}{t('inheriting', { count: selected.tenant_count })}</p>
                </div>
                <div className="plans-actions">
                  <button aria-label={t('rename')} className="icon-button" onClick={() => { setFormError(''); setGlossaryDraft({ id: selected.id, name: selected.name, description: selected.description, industry_code: selected.industry_code, row_version: selected.row_version }) }} title={t('rename')} type="button"><Pencil size={15} /></button>
                  <button aria-label={t('deleteGlossary')} className="icon-button" onClick={() => void removeGlossary(selected, false)} title={t('deleteGlossary')} type="button"><Trash2 size={15} /></button>
                  <button className="text-button" onClick={() => setImporting(true)} type="button"><Upload size={14} /> {t('import')}</button>
                  <button className="primary-button" onClick={() => { setFormError(''); setTermDraft({ id: null, term: '', aliases: '', definition: '', row_version: null }) }} type="button"><Plus size={15} /> {t('newTerm')}</button>
                </div>
              </div>
              <label className="search-field glossary-search"><Search size={14} /><input aria-label={t('search')} onChange={(event) => setSearch(event.target.value)} placeholder={t('search')} value={search} /></label>
              {terms && terms.items.length === 0 && <p className="assistants-empty-filtered">{search ? t('noMatch') : t('noTerms')}</p>}
              {terms && terms.items.length > 0 && (
                <div className="members-table-wrap">
                  <table className="members-table">
                    <thead><tr><th>{t('col.term')}</th><th>{t('col.aliases')}</th><th>{t('col.definition')}</th><th>{t('col.level')}</th><th /></tr></thead>
                    <tbody>
                      {terms.items.map((term) => (
                        <tr key={term.id}>
                          <td><strong>{term.term}</strong></td>
                          <td>{term.aliases.length ? term.aliases.join('、') : '—'}</td>
                          <td className="glossary-definition">{term.definition || '—'}</td>
                          <td><span className="assistant-status draft">{t('levelPlatform')}</span></td>
                          <td className="member-actions-cell">
                            <button aria-label={t('edit')} className="icon-button" onClick={() => { setFormError(''); setTermDraft({ id: term.id, term: term.term, aliases: term.aliases.join('；'), definition: term.definition, row_version: term.row_version }) }} title={t('edit')} type="button"><Pencil size={15} /></button>
                            <button aria-label={t('delete')} className="icon-button" onClick={() => setDeletingTerm(term)} title={t('delete')} type="button"><Trash2 size={15} /></button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {termDraft && (
        <div className="modal-backdrop" onClick={() => !saving && setTermDraft(null)} role="presentation">
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void saveTerm() }} role="dialog" aria-modal="true">
            <div className="section-kicker">PLATFORM</div>
            <h2>{t(termDraft.id ? 'editTerm' : 'newTerm')}</h2>
            <label>{t('field.term')} *<input autoFocus maxLength={200} onChange={(event) => setTermDraft({ ...termDraft, term: event.target.value })} required value={termDraft.term} /></label>
            <label>{t('field.aliases')}<input onChange={(event) => setTermDraft({ ...termDraft, aliases: event.target.value })} value={termDraft.aliases} /><small>{t('field.aliasesHint')}</small></label>
            <label>{t('field.definition')}<textarea maxLength={1000} onChange={(event) => setTermDraft({ ...termDraft, definition: event.target.value })} rows={3} value={termDraft.definition} /></label>
            {formError && <p className="members-error" role="alert">{formError}</p>}
            <div className="plans-actions">
              <button className="text-button" disabled={saving} onClick={() => setTermDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving || !termDraft.term.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
            </div>
          </form>
        </div>
      )}

      {glossaryDraft && (
        <div className="modal-backdrop" onClick={() => !saving && setGlossaryDraft(null)} role="presentation">
          <form className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void saveGlossary() }} role="dialog" aria-modal="true">
            <div className="section-kicker">PLATFORM</div>
            <h2>{t(glossaryDraft.id ? 'editGlossary' : 'newGlossary')}</h2>
            <label>{t('field.glossaryName')} *<input autoFocus maxLength={80} onChange={(event) => setGlossaryDraft({ ...glossaryDraft, name: event.target.value })} placeholder={t('field.glossaryNamePlaceholder')} required value={glossaryDraft.name} /></label>
            <label>{t('field.industry')}
              <select onChange={(event) => setGlossaryDraft({ ...glossaryDraft, industry_code: event.target.value })} value={glossaryDraft.industry_code}>
                {industries.filter((item) => item.status === 'active' || item.code === glossaryDraft.industry_code).map((item) => <option key={item.code} value={item.code}>{item.name_zh}</option>)}
              </select>
            </label>
            <label>{t('field.description')}<textarea maxLength={300} onChange={(event) => setGlossaryDraft({ ...glossaryDraft, description: event.target.value })} rows={2} value={glossaryDraft.description} /></label>
            {formError && <p className="members-error" role="alert">{formError}</p>}
            <div className="plans-actions">
              <button className="text-button" disabled={saving} onClick={() => setGlossaryDraft(null)} type="button">{t('cancel')}</button>
              <button className="primary-button" disabled={saving || !glossaryDraft.name.trim() || !glossaryDraft.industry_code} type="submit">{saving ? t('saving') : t(glossaryDraft.id ? 'save' : 'create')}</button>
            </div>
          </form>
        </div>
      )}

      {deletingTerm && <ConfirmDialog body={t('deleteTermBody', { name: deletingTerm.term })} danger onCancel={() => setDeletingTerm(null)} onConfirm={async () => { const target = deletingTerm; setDeletingTerm(null); await removeTerm(target) }} title={t('deleteTermTitle')} />}
      {deletingGlossary && (
        <ConfirmDialog
          body={deletingGlossary.tenant_count > 0 ? t('deleteGlossaryInUse', { count: deletingGlossary.tenant_count }) : t('deleteGlossaryBody', { name: deletingGlossary.name })} danger onCancel={() => setDeletingGlossary(null)}
          onConfirm={async () => { const target = deletingGlossary; setDeletingGlossary(null); await removeGlossary(target, true) }} title={t('deleteGlossaryTitle')}
        />
      )}
      {importing && selected && <TermImportDialog kicker="IMPORT" onClose={() => setImporting(false)} onDone={(result) => { setImporting(false); setNotice(t('imported', { count: (result.created ?? 0) + (result.updated ?? 0) })); loadTerms(); loadGlossaries() }} url={`/api/v1/platform/glossaries/${selected.id}/terms/import`} />}
      {managing && <IndustryManager industries={industries} onChanged={() => loadGlossaries()} onClose={() => setManaging(false)} />}
    </section>
  )
}
