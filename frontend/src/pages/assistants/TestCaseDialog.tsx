import { useState } from 'react'
import type { FormEvent } from 'react'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import { CASE_TYPES } from './testCaseTypes'

export type TestCase = {
  id: string
  name: string
  input: string
  expected: string
  type: (typeof CASE_TYPES)[number]
  status: 'not_run' | 'passed' | 'failed'
  run_state: 'idle' | 'queued' | 'running'
  last_output: string | null
  last_reason: string | null
  last_target: string | null
  last_run_at: string | null
  row_version: number
}

type Draft = Pick<TestCase, 'name' | 'input' | 'expected' | 'type'>

/** Create or edit a test case. ``initial.id`` set means editing; the console passes only ``input``. */
export function TestCaseDialog({
  assistantId,
  tenantId,
  initial,
  onClose,
  onSaved,
}: {
  assistantId: string
  tenantId: string
  initial: Partial<TestCase>
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation('testing')
  const [form, setForm] = useState<Draft>({
    name: initial.name ?? (initial.input ?? '').slice(0, 40),
    input: initial.input ?? '',
    expected: initial.expected ?? '',
    type: initial.type ?? 'regression',
  })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const path = `/api/v1/assistants/${assistantId}/test-cases${initial.id ? `/${initial.id}` : ''}`
      await apiRequest(path, {
        method: initial.id ? 'PUT' : 'POST',
        body: JSON.stringify({ ...form, row_version: initial.id ? initial.row_version : undefined }),
      }, tenantId)
      onSaved()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('saveFailed'))
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={onClose}>
      <form aria-labelledby="case-title" aria-modal="true" className="create-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => void submit(event)} role="dialog">
        <button aria-label={t('close')} className="modal-close" onClick={onClose} type="button"><X size={18} /></button>
        <h2 id="case-title">{initial.id ? t('dialog.edit') : t('dialog.create')}</h2>
        <label>{t('fields.name')}<input maxLength={100} onChange={(event) => setForm({ ...form, name: event.target.value })} required value={form.name} /></label>
        <label>{t('fields.input')}<textarea maxLength={2000} onChange={(event) => setForm({ ...form, input: event.target.value })} required rows={3} value={form.input} /></label>
        <label>{t('fields.expected')}<textarea maxLength={1000} onChange={(event) => setForm({ ...form, expected: event.target.value })} placeholder={t('fields.expectedHint')} required rows={3} value={form.expected} /></label>
        <label>{t('fields.type')}
          <select onChange={(event) => setForm({ ...form, type: event.target.value as Draft['type'] })} value={form.type}>
            {CASE_TYPES.map((key) => <option key={key} value={key}>{t(`types.${key}`)}</option>)}
          </select>
        </label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('cancel')}</button>
          <button className="primary-button" disabled={saving || !form.name.trim() || !form.input.trim() || !form.expected.trim()} type="submit">{saving ? t('saving') : t('save')}</button>
        </div>
      </form>
    </div>
  )
}
