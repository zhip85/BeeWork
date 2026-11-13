import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { StagePanelProps } from './stagePanels'

type FileConfig = { enabled: boolean; top_k: number; retention_days: number; write_enabled?: boolean; subscribe_enabled?: boolean }
type MemoryConfig = {
  short_term: { enabled: boolean; window_rounds: number; max_tokens: number; type_whitelist: string[] }
  long_term: { enabled: boolean; user_profile: FileConfig; user_memory: FileConfig; tools: FileConfig; session: FileConfig }
}
type Info = { tier: 'short' | 'short_long' | 'all_cross_assistant'; long_term_allowed: boolean; shared_profile_allowed: boolean }
type FileKey = 'user_profile' | 'user_memory' | 'tools' | 'session'

const FILES: FileKey[] = ['user_profile', 'user_memory', 'tools', 'session']
const TYPES = ['preference', 'history', 'fact'] as const
const DEFAULTS: MemoryConfig = {
  short_term: { enabled: true, window_rounds: 3, max_tokens: 5000, type_whitelist: ['preference', 'history'] },
  long_term: {
    enabled: true,
    user_profile: { enabled: true, top_k: 2, retention_days: 365, write_enabled: true, subscribe_enabled: true },
    user_memory: { enabled: true, top_k: 3, retention_days: 180 },
    tools: { enabled: true, top_k: 5, retention_days: 90 },
    session: { enabled: true, top_k: 5, retention_days: 30 },
  },
}

const clamp = (value: number, min: number, max: number, fallback: number) => Math.min(max, Math.max(min, Number.isFinite(value) && value > 0 ? Math.round(value) : fallback))

/** OC · memory (OC-020 – OC-022, T-056): the conversation window, the long-term files and what the plan allows. */
export function MemoryStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('orchestration')
  const stored = (oc.memory ?? {}) as Partial<MemoryConfig>
  const config: MemoryConfig = {
    short_term: { ...DEFAULTS.short_term, ...stored.short_term },
    long_term: {
      ...DEFAULTS.long_term, ...stored.long_term,
      user_profile: { ...DEFAULTS.long_term.user_profile, ...stored.long_term?.user_profile },
      user_memory: { ...DEFAULTS.long_term.user_memory, ...stored.long_term?.user_memory },
      tools: { ...DEFAULTS.long_term.tools, ...stored.long_term?.tools },
      session: { ...DEFAULTS.long_term.session, ...stored.long_term?.session },
    },
  }
  const [info, setInfo] = useState<Info | null>(null)
  const [editing, setEditing] = useState<FileKey | null>(null)
  const set = (next: MemoryConfig) => onChange({ ...oc, memory: next })
  const setShort = (patch: Partial<MemoryConfig['short_term']>) => set({ ...config, short_term: { ...config.short_term, ...patch } })
  const setLong = (patch: Partial<MemoryConfig['long_term']>) => set({ ...config, long_term: { ...config.long_term, ...patch } })
  const setFile = (key: FileKey, patch: Partial<FileConfig>) => setLong({ [key]: { ...config.long_term[key], ...patch } } as Partial<MemoryConfig['long_term']>)
  const longLocked = readOnly || (info !== null && !info.long_term_allowed)
  const longOff = longLocked || !config.long_term.enabled

  useEffect(() => {
    let active = true
    apiRequest<Info>(`/api/v1/assistants/${assistantId}/oc/memory/info`, {}, tenant.id).then((data) => active && setInfo(data)).catch(() => undefined)
    return () => { active = false }
  }, [assistantId, tenant.id])

  return (
    <div className="oc-rewrite-panel">
      <p className="usage-hint">{t('memory.hint')}</p>
      {info && <p className="assistants-notice" role="status">{t(`memory.tier.${info.tier}`)}</p>}

      <div className="panel rewrite-test">
        <h3>{t('memory.short.title')}</h3>
        <label className="governance-inherit">
          <input checked={config.short_term.enabled} disabled={readOnly} onChange={(event) => setShort({ enabled: event.target.checked })} type="checkbox" />
          <span><strong>{t('memory.short.enable')}</strong><small>{t('memory.short.enableHint')}</small></span>
        </label>
        <label className="rewrite-field">{t('memory.short.rounds')}
          <input disabled={readOnly || !config.short_term.enabled} max={30} min={1} onChange={(event) => setShort({ window_rounds: clamp(Number(event.target.value), 1, 30, 3) })} type="number" value={config.short_term.window_rounds} />
        </label>
        <label className="rewrite-field">{t('memory.short.tokens')}
          <input disabled={readOnly || !config.short_term.enabled} max={30000} min={1000} onChange={(event) => setShort({ max_tokens: clamp(Number(event.target.value), 1000, 30000, 5000) })} step={500} type="number" value={config.short_term.max_tokens} />
          <small>{t('memory.short.tokensHint')}</small>
        </label>
        <fieldset className="rewrite-steps" disabled={readOnly || !config.short_term.enabled}>
          <legend>{t('memory.short.types')}</legend>
          {TYPES.map((type) => (
            <label className="governance-inherit" key={type}>
              <input
                checked={config.short_term.type_whitelist.includes(type)}
                onChange={(event) => setShort({ type_whitelist: event.target.checked ? [...config.short_term.type_whitelist, type] : config.short_term.type_whitelist.filter((item) => item !== type) })}
                type="checkbox"
              />
              <span><strong>{t(`memory.type.${type}`)}</strong><small>{t(`memory.type.${type}Hint`)}</small></span>
            </label>
          ))}
        </fieldset>
      </div>

      <div className="panel rewrite-test">
        <h3>{t('memory.long.title')}</h3>
        {info && !info.long_term_allowed && <p className="form-error" role="status">{t('memory.long.locked')}</p>}
        <label className="governance-inherit">
          <input checked={config.long_term.enabled} disabled={longLocked} onChange={(event) => setLong({ enabled: event.target.checked })} type="checkbox" />
          <span><strong>{t('memory.long.enable')}</strong><small>{t('memory.long.enableHint')}</small></span>
        </label>
        <ul className="routing-rules">
          {FILES.map((key) => {
            const file = config.long_term[key]
            return (
              <li key={key}>
                <input aria-label={t('memory.file.enabled', { name: t(`memory.file.${key}.name`) })} checked={file.enabled} disabled={longOff} onChange={(event) => setFile(key, { enabled: event.target.checked })} type="checkbox" />
                <strong>{t(`memory.file.${key}.name`)}</strong>
                <span>{t(`memory.file.${key}.scope`)}</span>
                <span>{t('memory.file.summary', { k: file.top_k, days: file.retention_days })}</span>
                {key === 'user_profile' && info && !info.shared_profile_allowed && <span className="wizard-muted">{t('memory.file.profilePrivate')}</span>}
                {!longOff && <button className="text-button" onClick={() => setEditing(key)} type="button">{t('memory.file.configure')}</button>}
              </li>
            )
          })}
        </ul>
      </div>

      <div className="panel rewrite-test">
        <h3>{t('memory.classes.title')}</h3>
        <p className="usage-hint">{t('memory.classes.hint')}</p>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>{t('memory.classes.class')}</th><th>{t('memory.classes.scope')}</th><th>{t('memory.classes.source')}</th><th>{t('memory.classes.access')}</th><th>{t('memory.classes.retention')}</th></tr></thead>
            <tbody>
              {FILES.map((key) => (
                <tr key={key}>
                  <td>{t(`memory.file.${key}.name`)}</td><td>{t(`memory.file.${key}.scope`)}</td><td>{t(`memory.file.${key}.source`)}</td>
                  <td>{t(`memory.file.${key}.access`)}</td><td>{t('memory.classes.retentionValue', { days: config.long_term[key].retention_days })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="usage-hint">{t('memory.classes.privacy')}</p>
      </div>

      {editing && <FileDialog fileKey={editing} file={config.long_term[editing]} onClose={() => setEditing(null)} onSave={(patch) => { setFile(editing, patch); setEditing(null) }} />}
    </div>
  )
}

function FileDialog({ fileKey, file, onSave, onClose }: { fileKey: FileKey; file: FileConfig; onSave: (patch: Partial<FileConfig>) => void; onClose: () => void }) {
  const { t } = useTranslation('orchestration')
  const [draft, setDraft] = useState(file)
  return (
    <div className="modal-backdrop confirm-backdrop" onClick={onClose} role="presentation">
      <form aria-label={t(`memory.file.${fileKey}.name`)} className="create-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); onSave(draft) }} role="dialog">
        <h2>{t('memory.dialog.title', { name: t(`memory.file.${fileKey}.name`) })}</h2>
        <p className="wizard-muted">{t(`memory.file.${fileKey}.help`)}</p>
        <label className="kb-field"><span>{t('memory.dialog.topK')}</span><input max={10} min={1} onChange={(event) => setDraft({ ...draft, top_k: clamp(Number(event.target.value), 1, 10, 3) })} type="number" value={draft.top_k} /><small>{t('memory.dialog.topKHint')}</small></label>
        <label className="kb-field"><span>{t('memory.dialog.retention')}</span><input max={3650} min={1} onChange={(event) => setDraft({ ...draft, retention_days: clamp(Number(event.target.value), 1, 3650, 180) })} type="number" value={draft.retention_days} /><small>{t('memory.dialog.retentionHint')}</small></label>
        {fileKey === 'user_profile' && (
          <>
            <label className="governance-inherit"><input checked={draft.write_enabled ?? true} onChange={(event) => setDraft({ ...draft, write_enabled: event.target.checked })} type="checkbox" /><span><strong>{t('memory.dialog.write')}</strong><small>{t('memory.dialog.writeHint')}</small></span></label>
            <label className="governance-inherit"><input checked={draft.subscribe_enabled ?? true} onChange={(event) => setDraft({ ...draft, subscribe_enabled: event.target.checked })} type="checkbox" /><span><strong>{t('memory.dialog.subscribe')}</strong><small>{t('memory.dialog.subscribeHint')}</small></span></label>
          </>
        )}
        <div className="confirm-actions">
          <button className="secondary-button" onClick={onClose} type="button">{t('memory.dialog.cancel')}</button>
          <button className="primary-button" type="submit">{t('memory.dialog.save')}</button>
        </div>
      </form>
    </div>
  )
}
