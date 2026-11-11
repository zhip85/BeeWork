import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, DragEvent, FormEvent } from 'react'
import { AlertTriangle, ArrowLeft, CheckCircle2, CircleDashed, Loader2, ShieldCheck, Upload, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import type { Capability } from './types'

const MAX_BYTES = 20 * 1024 * 1024
const ACCEPT = '.zip,.skill,.md'

async function uploadRaw(path: string, file: File, tenantId: string): Promise<Capability> {
  return apiRequest<Capability>(path, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } }, tenantId)
}

function frontmatter(text: string): { name?: string; description?: string } {
  const match = /^﻿?---\s*\n([\s\S]*?)\n---/.exec(text)
  if (!match) return {}
  const out: Record<string, string> = {}
  for (const line of match[1].split('\n')) {
    const pair = /^(name|description):\s*(.*)$/.exec(line.trim())
    if (pair) out[pair[1]] = pair[2].replace(/^['"]|['"]$/g, '')
  }
  return out
}

/** Upload / edit a Skill (CAP-020–026). The four checks run in the worker; this page polls them (N/4). Built-in skills
 * are read-only. */
export function SkillFormPage({ tenant }: { tenant: TenantMembership }) {
  const { t } = useTranslation('capabilities')
  const { capabilityId } = useParams()
  const navigate = useNavigate()
  const creating = !capabilityId
  const writable = tenant.access_mode === 'full' && tenant.permissions.includes('capabilities:edit')
  const [cap, setCap] = useState<Capability | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [loading, setLoading] = useState(!creating)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const input = useRef<HTMLInputElement>(null)

  const apply = useCallback((next: Capability) => { setCap(next); setName(next.name); setDescription(next.description) }, [])

  useEffect(() => {
    if (!capabilityId) return
    apiRequest<Capability>(`/api/v1/capabilities/${capabilityId}`, {}, tenant.id)
      .then(apply)
      .catch((requestError: Error) => setError(requestError.message || t('loadFailed')))
      .finally(() => setLoading(false))
  }, [capabilityId, tenant.id, apply, t])

  const checking = cap?.skill && ['pending', 'running'].includes(cap.skill.security_status)
  useEffect(() => {
    if (!checking || !cap) return
    const timer = window.setInterval(() => {
      apiRequest<Capability>(`/api/v1/capabilities/${cap.id}`, {}, tenant.id).then((next) => {
        setCap(next)
        if (next.skill?.security_status === 'passed') setNotice(next.status === 'available' ? t('skill.passedAvailable') : next.pending_approval ? t('skill.passedPending') : t('skill.passed'))
      }).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [checking, cap, tenant.id, t])

  async function pick(selected: File | undefined | null) {
    setError('')
    if (!selected) return
    if (!/\.(zip|skill|md)$/i.test(selected.name)) { setError(t('skill.badType')); return }
    if (selected.size > MAX_BYTES) { setError(t('skill.tooLarge')); return }
    setFile(selected)
    if (creating && !name.trim()) setName(selected.name.replace(/\.[^.]+$/, '').slice(0, 50))  // CAP-021
    if (/\.md$/i.test(selected.name)) {
      const meta = frontmatter(await selected.text())
      if (meta.name && creating) setName(meta.name.slice(0, 50))
      if (meta.description && creating && !description.trim()) setDescription(meta.description.slice(0, 500))
    }
  }

  const onDrop = (event: DragEvent) => { event.preventDefault(); void pick(event.dataTransfer.files?.[0]) }
  const onChange = (event: ChangeEvent<HTMLInputElement>) => void pick(event.target.files?.[0])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError(''); setNotice('')
    try {
      if (creating) {
        if (!file) return
        const query = new URLSearchParams({ filename: file.name, name: name.trim(), description: description.trim() })
        const created = await uploadRaw(`/api/v1/capabilities/skills?${query}`, file, tenant.id)
        navigate(`/capabilities/skill/${created.id}`, { replace: true })
        apply(created)
        setFile(null)
      } else if (cap) {
        let current = cap
        if (name.trim() !== cap.name || description.trim() !== cap.description) {
          current = await apiRequest<Capability>(`/api/v1/capabilities/skills/${cap.id}`, { method: 'PUT', body: JSON.stringify({ name: name.trim(), description: description.trim(), row_version: cap.row_version }) }, tenant.id)
        }
        if (file) current = await uploadRaw(`/api/v1/capabilities/skills/${cap.id}/package?${new URLSearchParams({ filename: file.name })}`, file, tenant.id)  // CAP-025: a new file is checked again
        apply(current)
        setFile(null)
        setNotice(t('saved'))
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setBusy(false)
    }
  }

  async function recheck() {
    if (!cap) return
    setBusy(true); setError('')
    try { apply(await apiRequest<Capability>(`/api/v1/capabilities/skills/${cap.id}/recheck`, { method: 'POST' }, tenant.id)) } catch (requestError) { setError(requestError instanceof Error ? requestError.message : t('actionFailed')) } finally { setBusy(false) }
  }

  if (loading) return <section className="capability-form"><p>{t('loading')}</p></section>
  if (!creating && !cap) return <section className="capability-form"><p className="form-error" role="alert">{error || t('notFound')}</p></section>

  const skill = cap?.skill ?? null
  const builtin = Boolean(skill?.builtin)
  const editable = writable && !builtin
  const done = skill ? skill.checks.filter((check) => !['pending', 'running'].includes(check.status)).length : 0

  return (
    <section className="kb-create-page capability-form">
      <Link className="back-link" to="/capabilities?type=skill"><ArrowLeft size={15} /> {t('backTo', { kind: t('kind.skill') })}</Link>
      <div className="kb-create-shell">
        <header className="kb-create-title">
          <div>
            <span className="eyebrow">CAPABILITY</span>
            <h1>{creating ? t('create.skill') : t('editTitle', { kind: t('kind.skill') })}</h1>
            <p>{t('kindHint.skill')}</p>
          </div>
          {cap && <span className={`assistant-status flows-status-${cap.status === 'available' ? 'published' : cap.status}`}>{t(`status.${cap.status}`)}{cap.pending_approval ? ` · ${t('pendingApproval')}` : ''}</span>}
        </header>
        <form className="kb-create-form" onSubmit={(event) => void submit(event)}>
          {builtin && <p className="assistants-notice"><ShieldCheck size={15} /> {t('skill.builtinNote')}{skill && !skill.platform_active ? ` ${t('skill.platformDisabled')}` : ''}</p>}
          {editable && (
            <div className={`skill-drop${checking ? ' disabled' : ''}`} onClick={() => !checking && input.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={onDrop} role="button" tabIndex={0}>
              <Upload size={20} />
              <strong>{file ? file.name : skill?.filename ? t('skill.replace', { name: skill.filename }) : t('skill.drop')}</strong>
              <small>{t('skill.dropHint')}</small>
              <input accept={ACCEPT} disabled={Boolean(checking)} hidden onChange={onChange} ref={input} type="file" />
            </div>
          )}
          <label className="kb-field"><span>{t('form.name')} <em>*</em></span><input disabled={!editable} maxLength={50} onChange={(event) => setName(event.target.value)} required value={name} /></label>
          <label className="kb-field"><span>{t('form.description')}</span><textarea disabled={!editable} maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder={t('form.descriptionHint')} rows={3} value={description} /></label>
          {skill && !builtin && (
            <div className="skill-checks">
              <div className="capability-tools-head">
                <span>{checking ? t('skill.checking', { done, total: 4 }) : skill.security_status === 'passed' ? t('skill.allPassed') : skill.security_status === 'failed' ? t('skill.failed') : t('skill.notChecked')}</span>
                {editable && skill.security_status === 'failed' && <button className="secondary-button" disabled={busy} onClick={() => void recheck()} type="button">{t('skill.recheck')}</button>}
              </div>
              <p className="wizard-muted">{t('skill.gate')}</p>
              <ol>
                {skill.checks.map((check) => (
                  <li className={`skill-check ${check.status}`} key={check.key}>
                    <span className="skill-check-icon">
                      {check.status === 'passed' ? <CheckCircle2 size={15} /> : check.status === 'failed' ? <XCircle size={15} /> : check.status === 'warning' ? <AlertTriangle size={15} /> : check.status === 'running' ? <Loader2 className="spin" size={15} /> : <CircleDashed size={15} />}
                    </span>
                    <strong>{t(`skill.check.${check.key}`)}</strong>
                    <em>{t(`skill.state.${check.status}`)}</em>
                    {check.issues.length > 0 && (
                      <ul>{check.issues.map((issue, index) => <li key={index}><code>{String(issue.path)}</code> {String(issue.reason)}</li>)}</ul>
                    )}
                  </li>
                ))}
              </ol>
              {skill.enable_warning && <p className="form-error" role="alert">{skill.enable_warning}</p>}
              {skill.files.length > 0 && <details><summary>{t('skill.files', { count: skill.files.length })}</summary><ul className="skill-files">{skill.files.map((entry) => <li key={String(entry.path)}><code>{String(entry.path)}</code><small>{Number(entry.size).toLocaleString()} B</small></li>)}</ul></details>}
            </div>
          )}
          {notice && <p className="assistants-notice" role="status">{notice}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="kb-form-actions">
            <Link className="kb-cancel" to="/capabilities?type=skill">{t('cancel')}</Link>
            {editable && (
              <button className="primary-button" disabled={busy || Boolean(checking) || !name.trim() || (creating && !file)} type="submit">
                {checking ? t('skill.checkingButton') : busy ? t('saving') : creating ? t('skill.uploadAndCheck') : file ? t('skill.uploadAndCheck') : t('save')}
              </button>
            )}
          </div>
        </form>
      </div>
    </section>
  )
}
