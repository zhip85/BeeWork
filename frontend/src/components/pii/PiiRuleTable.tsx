import { Lock, Pencil, RotateCcw, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { RuleMode, RuleRow } from './types'

/** The PII rule table shared by the platform, tenant and assistant pages (PRD 3.6.3 / 3.10.6 / 3.16.2). */
export function PiiRuleTable({ mode, rows, readOnly = false, detectionOff = false, onToggle, onEdit, onRestore, onDelete }: {
  mode: RuleMode
  rows: RuleRow[]
  readOnly?: boolean
  detectionOff?: boolean
  onToggle: (row: RuleRow) => void
  onEdit: (row: RuleRow) => void
  onRestore?: (row: RuleRow) => void
  onDelete?: (row: RuleRow) => void
}) {
  const { t } = useTranslation('governance')
  return (
    <div className="members-table-wrap">
      <table className="members-table">
        <thead><tr><th>{t('col.rule')}</th><th>{t('col.pattern')}</th><th>{t('col.stage')}</th><th>{t('col.action')}</th>{mode !== 'platform' && <th>{t('col.layer')}</th>}<th>{t('col.enabled')}</th><th /></tr></thead>
        <tbody>
          {rows.map((row) => {
            const off = !row.enabled || row.switchedOff
            return (
              <tr className={off ? 'governance-off' : ''} key={row.key}>
                <td>
                  <span className="governance-name">{row.enforced && <Lock aria-label={t('enforced')} size={13} />}<strong>{row.name}</strong></span>
                  {row.pending && <small className="tenant-status-note">{t('pendingReview')}</small>}
                  {row.switchedOff && detectionOff && <small>{t('detectionOffNote')}</small>}
                </td>
                <td><code className="governance-pattern">{row.pattern || '—'}</code></td>
                <td>{t(`stage.${row.stage}`)}</td>
                <td>{t(`action.${row.action}`)}{row.action === 'block' && <small>{row.block_message || t('defaultBlockMessage')}</small>}</td>
                {mode !== 'platform' && <td>{t(`layer.${row.label}`)}</td>}
                <td>
                  <label className="governance-switch">
                    <input aria-label={t('col.enabled')} checked={row.enabled && !row.switchedOff} disabled={readOnly || row.pending || row.switchedOff || (row.enforced && mode !== 'platform')} onChange={() => onToggle(row)} type="checkbox" />
                    {row.enforced && <span className="assistant-status published">{t('enforced')}</span>}
                  </label>
                </td>
                <td className="member-actions-cell">
                  {!readOnly && <button aria-label={t('edit')} className="icon-button" disabled={row.pending} onClick={() => onEdit(row)} title={t('edit')} type="button"><Pencil size={15} /></button>}
                  {!readOnly && row.restorable && onRestore && <button aria-label={t('restore')} className="icon-button" disabled={row.pending} onClick={() => onRestore(row)} title={t('restore')} type="button"><RotateCcw size={15} /></button>}
                  {!readOnly && row.deletable && onDelete && <button aria-label={t('delete')} className="icon-button" disabled={row.pending} onClick={() => onDelete(row)} title={t('delete')} type="button"><Trash2 size={15} /></button>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
