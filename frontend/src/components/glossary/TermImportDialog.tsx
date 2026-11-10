import { useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'

export type ImportResult = {
  recognized: number
  duplicates: number
  created?: number
  updated?: number
  errors: { line: number; message: string }[]
  preview: { line: number; term: string; aliases: string[]; definition: string; exists: boolean }[]
  outcome?: 'preview' | 'applied' | 'pending'
}

/** Glossary import dialog (PLT-124 / SET-063): choose a file or paste text, see the recognised count live, pick what to do with duplicates. */
export function TermImportDialog({ url, tenantId, kicker, onClose, onDone }: {
  url: string
  tenantId?: string
  kicker: string
  onClose: () => void
  onDone: (result: ImportResult) => void
}) {
  const { t } = useTranslation('glossary')
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<ImportResult | null>(null)
  const [onDuplicate, setOnDuplicate] = useState<'skip' | 'overwrite'>('skip')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!text.trim()) return
    const timer = window.setTimeout(() => {
      apiRequest<ImportResult>(`${url}?dry_run=true&on_duplicate=${onDuplicate}`, { method: 'POST', body: text, headers: { 'Content-Type': 'text/plain' } }, tenantId)
        .then((result) => { setPreview(result); setError('') })
        .catch((requestError: Error) => { setPreview(null); setError(requestError.message) })
    }, 300)
    return () => window.clearTimeout(timer)
  }, [text, onDuplicate, url, tenantId])

  async function chooseFile(list: FileList | null) {
    const file = list?.[0]
    if (fileInput.current) fileInput.current.value = ''
    if (!file) return
    const buffer = await file.arrayBuffer()
    try {
      setText(new TextDecoder('utf-8', { fatal: true }).decode(buffer))
    } catch {
      setText(new TextDecoder('gb18030').decode(buffer))
    }
  }

  async function run() {
    setBusy(true)
    try {
      onDone(await apiRequest<ImportResult>(`${url}?on_duplicate=${onDuplicate}`, { method: 'POST', body: text, headers: { 'Content-Type': 'text/plain' } }, tenantId))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div aria-modal="true" className="create-modal governance-modal" onClick={(event) => event.stopPropagation()} role="dialog">
        <div className="section-kicker">{kicker}</div>
        <h2>{t('importTitle')}</h2>
        <p className="usage-hint">{t('importHint')}</p>
        <div className="plans-actions">
          <button className="text-button" onClick={() => fileInput.current?.click()} type="button"><Download size={14} /> {t('chooseFile')}</button>
          <input accept=".csv,.txt,text/csv,text/plain" hidden onChange={(event) => void chooseFile(event.target.files)} ref={fileInput} type="file" />
        </div>
        <label>{t('orPaste')}<textarea className="governance-mono" onChange={(event) => setText(event.target.value)} rows={6} value={text} /></label>
        {preview && text.trim() && (
          <>
            <p role="status"><strong>{t('recognized', { count: preview.recognized })}</strong>{preview.duplicates > 0 && ` · ${t('duplicates', { count: preview.duplicates })}`}</p>
            {preview.duplicates > 0 && (
              <fieldset className="governance-ruleset">
                <legend>{t('duplicateHandling')}</legend>
                {(['skip', 'overwrite'] as const).map((value) => (
                  <label className="governance-check" key={value}><input checked={onDuplicate === value} name="dup" onChange={() => setOnDuplicate(value)} type="radio" /> {t(`duplicate.${value}`)}</label>
                ))}
              </fieldset>
            )}
            {preview.errors.length > 0 && (
              <ul className="form-error" aria-label={t('importErrors')}>
                {preview.errors.map((item) => <li key={`${item.line}-${item.message}`}>{t('line', { line: item.line })}：{item.message}</li>)}
              </ul>
            )}
          </>
        )}
        {error && <p className="members-error" role="alert">{error}</p>}
        <div className="plans-actions">
          <button className="text-button" onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={busy || !text.trim() || !preview || preview.recognized === 0} onClick={() => void run()} type="button">{t('importConfirm')}</button>
        </div>
      </div>
    </div>
  )
}
