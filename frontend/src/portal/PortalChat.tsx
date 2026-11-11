import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { ArrowUp, RotateCcw } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiRequestError } from '../api'
import { usePortal } from './portalContext'
import type { PortalConversation, PortalMessage } from './portalTypes'

type Shown = { key: string; role: 'user' | 'assistant'; content: string; citations?: PortalMessage['citations'] }

/** One conversation: the history, a streaming answer and the composer. With no `conversationId` it is the “new chat”
 * screen (greeting + centred input). Answers come from the real runtime over SSE — nothing here invents a reply. */
export default function PortalChat({ conversationId, projectId }: { conversationId?: string; projectId?: string }) {
  const { t } = useTranslation('portalApp')
  const portal = usePortal()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [shown, setShown] = useState<Shown[]>([])
  const [conversation, setConversation] = useState<PortalConversation | null>(null)
  const [text, setText] = useState('')
  const [appId, setAppId] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<{ message: string; retry: string } | null>(null)
  const [loadError, setLoadError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const request = portal.request

  const load = useCallback(() => {
    if (!conversationId) return
    request<{ conversation: PortalConversation; messages: PortalMessage[] }>(`/api/v1/portal/me/conversations/${conversationId}/messages`)
      .then((result) => {
        setConversation(result.conversation)
        setShown(result.messages.map((m) => ({ key: m.id, role: m.role, content: m.content, citations: m.citations })))
        setLoadError('')
      })
      .catch((failure: Error) => setLoadError(failure.message))
  }, [conversationId, request])
  useEffect(() => { setShown([]); setConversation(null); setError(null); load() }, [load])
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [shown])
  // `?app=` comes from the library; only an app the employee can actually use is accepted.
  const requestedApp = searchParams.get('app')
  useEffect(() => {
    if (appId || portal.apps.length === 0) return
    setAppId(portal.apps.find((app) => app.id === requestedApp)?.id ?? portal.apps[0].id)
  }, [appId, portal.apps, requestedApp])

  const activeAppId = conversation?.app_id ?? appId
  const readonly = Boolean(conversation?.readonly)

  async function send(message: string) {
    const content = message.trim()
    if (!content || sending || !activeAppId) return
    setSending(true)
    setError(null)
    setText('')
    const pending = `pending-${Date.now()}`
    setShown((current) => [...current, { key: `${pending}-u`, role: 'user', content }, { key: `${pending}-a`, role: 'assistant', content: '' }])
    let newConversation = conversationId ?? ''
    let failed = false
    try {
      await portal.streamChat(
        { app_id: activeAppId, message: content, conversation_id: conversationId, project_id: conversationId ? undefined : projectId },
        (name, data) => {
          if (name === 'start') newConversation = String(data.conversation_id)
          else if (name === 'delta') setShown((current) => current.map((m) => (m.key === `${pending}-a` ? { ...m, content: m.content + String(data.text ?? '') } : m)))
          else if (name === 'done') {
            const message = data.message as { content?: string } | undefined
            const citations = (data.citations as PortalMessage['citations'] | undefined) ?? []
            setShown((current) => current.map((m) => (m.key === `${pending}-a` ? { ...m, content: message?.content ?? m.content, citations } : m)))
          } else if (name === 'error') {
            failed = true
            setError({ message: String(data.message ?? t('chat.failed')), retry: content })
          }
        },
      )
    } catch (failure) {
      failed = true
      const unavailable = failure instanceof ApiRequestError && failure.code === 'portal.app_unavailable'
      setError({ message: unavailable ? t('chat.unavailable') : failure instanceof Error && failure.message ? failure.message : t('chat.failed'), retry: unavailable ? '' : content })
      setShown((current) => current.filter((m) => !m.key.startsWith(pending)))
    } finally {
      setSending(false)
      portal.reload()
    }
    if (failed) {
      setShown((current) => current.filter((m) => !(m.key === `${pending}-a` && m.content === '')))
      return
    }
    if (!conversationId && newConversation) navigate(`/portal/c/${newConversation}`, { replace: true })
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send(text)
    }
  }

  const empty = !conversationId && shown.length === 0
  const hour = new Date().getHours()
  const part = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening'
  const composer = (
    <form className="portal-composer" onSubmit={(event) => { event.preventDefault(); void send(text) }}>
      <textarea
        aria-label={t('inputLabel')}
        disabled={readonly || portal.apps.length === 0}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={portal.config.input_placeholder || t('askPrompt')}
        rows={empty ? 3 : 2}
        value={text}
      />
      <div className="portal-composer-bar">
        {!conversationId && (
          <label className="portal-select">
            <span>{t('appPicker')}</span>
            <select disabled={sending || portal.apps.length === 0} onChange={(event) => setAppId(event.target.value)} value={appId}>
              {portal.apps.length === 0 && <option value="">{t('noApps')}</option>}
              {portal.apps.map((app) => <option key={app.id} value={app.id}>{app.name}</option>)}
            </select>
          </label>
        )}
        {conversationId && conversation?.app_name && <small className="portal-hint">{conversation.app_name}</small>}
        <button aria-label={t('send')} className="portal-send" disabled={sending || readonly || !text.trim() || !activeAppId} type="submit"><ArrowUp size={16} /></button>
      </div>
    </form>
  )

  if (empty) {
    return (
      <section className="portal-home">
        <h1>{t(`greeting.${part}`, { name: portal.userName })}</h1>
        <p className="portal-home-sub">{t('askPrompt')}</p>
        {composer}
        {portal.apps.length === 0 && <p className="portal-hint" role="status">{t('noAppsHint')}</p>}
        {error && <ChatError error={error} onRetry={(message) => void send(message)} />}
      </section>
    )
  }
  return (
    <section className="portal-chat">
      {loadError && <p className="portal-error" role="alert">{loadError}</p>}
      <div className="portal-messages" aria-live="polite">
        {shown.map((message) => (
          <div className={`portal-msg ${message.role}`} key={message.key}>
            <p>{message.content || (sending ? t('chat.thinking') : '')}</p>
            {message.citations && message.citations.length > 0 && (
              <ul className="portal-citations">{message.citations.map((c, index) => <li key={index}>{c.document}{c.snippet ? ` — ${c.snippet}` : ''}</li>)}</ul>
            )}
          </div>
        ))}
        {error && <ChatError error={error} onRetry={(message) => void send(message)} />}
        <div ref={endRef} />
      </div>
      {readonly && <p className="portal-banner" role="status">{t('chat.readonly')}</p>}
      <div className="portal-chat-composer">{composer}</div>
    </section>
  )
}

function ChatError({ error, onRetry }: { error: { message: string; retry: string }; onRetry: (message: string) => void }) {
  const { t } = useTranslation('portalApp')
  return (
    <div className="portal-error portal-chat-error" role="alert">
      <span>{error.message}</span>
      {error.retry && <button onClick={() => onRetry(error.retry)} type="button"><RotateCcw size={13} /> {t('chat.retry')}</button>}
    </div>
  )
}
