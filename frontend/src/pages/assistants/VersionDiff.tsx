import { useTranslation } from 'react-i18next'

export type VersionChange = {
  section: string
  field: string
  from: unknown
  to: unknown
  lines?: string[]
}

const show = (value: unknown) => (typeof value === 'string' ? value : JSON.stringify(value) ?? '—')

/** Differences grouped by section; prompt changes show a line diff. */
export function ChangeList({ changes }: { changes: VersionChange[] }) {
  const { t } = useTranslation('versions')
  return (
    <ul className="version-changes">
      {changes.map((change) => (
        <li key={`${change.section}:${change.field}`}>
          <strong>{t(`compare.sections.${change.section}`, { defaultValue: change.section })}</strong>
          <small>{change.field}</small>
          {change.lines && change.lines.length > 0
            ? <pre className="version-lines">{change.lines.join('\n')}</pre>
            : <p><span className="diff-from">{t('compare.from_label')}: {show(change.from)}</span><span className="diff-to">{t('compare.to_label')}: {show(change.to)}</span></p>}
        </li>
      ))}
    </ul>
  )
}
