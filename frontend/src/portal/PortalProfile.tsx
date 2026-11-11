import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { usePortal } from './portalContext'

type Profile = {
  display_name: string; email: string; department: string; job_title: string
  default_app_id: string | null; default_app_name: string | null; can_edit: boolean; login_method: string
  memory_available: boolean; memory_enabled: boolean; notifications_available: boolean; notifications_enabled: boolean
}
type Memory = { enabled: boolean; items: { id: string; kind: string; content: string; created_at: string }[] }

/** Personal center (演进 PRD 11.6): reached only from the account menu. Everything here is the signed-in employee's own. */
export function PortalProfile({ onLogout }: { onLogout: () => void }) {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const request = portal.request
  const [profile, setProfile] = useState<Profile | null>(null)
  const [name, setName] = useState('')
  const [title, setTitle] = useState('')
  const [defaultApp, setDefaultApp] = useState('')
  const [memory, setMemory] = useState<Memory | null>(null)
  const [confirmErase, setConfirmErase] = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    request<Profile>('/api/v1/portal/me/profile')
      .then((result) => {
        setProfile(result); setName(result.display_name); setTitle(result.job_title); setDefaultApp(result.default_app_id ?? '')
        if (result.memory_available) request<Memory>('/api/v1/portal/me/memory').then(setMemory).catch(() => undefined)
      })
      .catch((failure: Error) => setError(failure.message))
  }, [request])
  useEffect(() => { load() }, [load])

  const fail = (failure: unknown) => { setNotice(''); setError(failure instanceof Error ? failure.message : t('profile.failed')) }
  const done = (message: string) => { setError(''); setNotice(message) }

  async function save(event: FormEvent) {
    event.preventDefault()
    try {
      const result = await request<Profile>('/api/v1/portal/me/profile', { method: 'PUT', body: JSON.stringify({ display_name: name, job_title: title, default_app_id: defaultApp || null }) })
      setProfile(result); done(t('profile.saved')); portal.reload()
    } catch (failure) { fail(failure) }
  }
  async function switchMemory(enabled: boolean) {
    try {
      setMemory(await request<Memory>('/api/v1/portal/me/memory/settings', { method: 'PUT', body: JSON.stringify({ enabled }) }))
      done(enabled ? t('profile.memoryOn') : t('profile.memoryOff'))
    } catch (failure) { fail(failure) }
  }
  async function erase() {
    try {
      await request('/api/v1/portal/me/memory', { method: 'DELETE' })
      setConfirmErase(false); done(t('profile.memoryErased')); load()
    } catch (failure) { fail(failure) }
  }
  async function switchNotifications(enabled: boolean) {
    try {
      setProfile(await request<Profile>('/api/v1/portal/me/notifications', { method: 'PUT', body: JSON.stringify({ enabled }) }))
      done(t('profile.saved'))
    } catch (failure) { fail(failure) }
  }
  async function changePassword(event: FormEvent) {
    event.preventDefault()
    try {
      await request('/api/v1/portal/me/password', { method: 'POST', body: JSON.stringify({ current_password: current, new_password: next }) })
      setCurrent(''); setNext(''); done(t('profile.passwordChanged'))
    } catch (failure) { fail(failure) }
  }

  if (!profile) return <section className="portal-page"><h1>{t('profile.title')}</h1>{error ? <p className="portal-error" role="alert">{error}</p> : <p>{t('loading')}</p>}</section>
  return (
    <section className="portal-page portal-profile">
      <h1>{t('profile.title')}</h1>
      {notice && <p className="portal-banner" role="status">{notice}</p>}
      {error && <p className="portal-error" role="alert">{error}</p>}

      <form className="portal-panel" onSubmit={(event) => void save(event)}>
        <h2>{t('profile.basics')}</h2>
        {!profile.can_edit && <p className="portal-hint">{t('profile.readOnly')}</p>}
        <label><span>{t('profile.name')}</span><input disabled={!profile.can_edit} maxLength={120} onChange={(event) => setName(event.target.value)} required value={name} /></label>
        <label><span>{t('profile.email')}</span><input disabled value={profile.email} /></label>
        <label><span>{t('profile.department')}</span><input disabled value={profile.department} /></label>
        <label><span>{t('profile.jobTitle')}</span><input disabled={!profile.can_edit} maxLength={60} onChange={(event) => setTitle(event.target.value)} value={title} /></label>
        <label>
          <span>{t('profile.defaultApp')}</span>
          <select disabled={!profile.can_edit} onChange={(event) => setDefaultApp(event.target.value)} value={defaultApp}>
            <option value="">{t('profile.noDefault')}</option>
            {portal.apps.map((app) => <option key={app.id} value={app.id}>{app.name}</option>)}
          </select>
        </label>
        {profile.can_edit && <button className="portal-primary" disabled={!name.trim()} type="submit">{t('project.save')}</button>}
      </form>

      {profile.memory_available && memory && (
        <div className="portal-panel">
          <h2>{t('profile.memory')}</h2>
          <p className="portal-hint">{t('profile.memoryHint')}</p>
          <label className="portal-check"><input checked={memory.enabled} onChange={(event) => void switchMemory(event.target.checked)} type="checkbox" />{t('profile.memorySwitch')}</label>
          {memory.items.length === 0 ? <p className="portal-hint">{t('profile.memoryEmpty')}</p> : (
            <ul className="portal-memory">{memory.items.map((item) => <li key={item.id}><small>{t(`profile.kinds.${item.kind}`)}</small>{item.content}</li>)}</ul>
          )}
          {memory.items.length > 0 && (confirmErase ? (
            <p><small className="portal-hint">{t('profile.eraseConfirm')}</small>{' '}<button className="portal-link portal-linkbutton danger" onClick={() => void erase()} type="button">{t('conv.deleteYes')}</button>{' '}<button className="portal-link portal-linkbutton" onClick={() => setConfirmErase(false)} type="button">{t('conv.cancel')}</button></p>
          ) : <button className="portal-link portal-linkbutton danger" onClick={() => setConfirmErase(true)} type="button">{t('profile.erase')}</button>)}
        </div>
      )}

      {profile.notifications_available && (
        <div className="portal-panel">
          <h2>{t('profile.notifications')}</h2>
          <label className="portal-check"><input checked={profile.notifications_enabled} onChange={(event) => void switchNotifications(event.target.checked)} type="checkbox" />{t('profile.notificationsSwitch')}</label>
        </div>
      )}

      <form className="portal-panel" onSubmit={(event) => void changePassword(event)}>
        <h2>{t('profile.security')}</h2>
        <p className="portal-hint">{t('profile.loginMethod', { method: profile.login_method })}</p>
        <label><span>{t('profile.currentPassword')}</span><input autoComplete="current-password" onChange={(event) => setCurrent(event.target.value)} required type="password" value={current} /></label>
        <label><span>{t('profile.newPassword')}</span><input autoComplete="new-password" minLength={12} onChange={(event) => setNext(event.target.value)} required type="password" value={next} /><small className="portal-hint">{t('profile.passwordHint')}</small></label>
        <button className="portal-primary" disabled={!current || next.length < 12} type="submit">{t('profile.changePassword')}</button>
        <button className="portal-link portal-linkbutton" onClick={onLogout} type="button">{t('logout')}</button>
      </form>
    </section>
  )
}
