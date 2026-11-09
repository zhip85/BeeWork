import { useEffect, useRef, useState } from 'react'
import { ExternalLink, FlaskConical, RotateCcw, Send, Square } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { TenantMembership } from '../../types'
import { streamRunEvents } from './runStream'
import type { RunEvent } from './runStream'
import { TestCaseDialog } from './TestCaseDialog'
import type { Assistant } from './types'

type Citation = { document_name: string; kb_name?: string; kind?: string; snippet?: string }
type Meta = { latencyMs?: number; tokensIn?: number | null; tokensOut?: number | null; traceUrl?: string | null; retrieval?: string; citations?: Citation[] }
type Message = { id: number; role: 'user' | 'assistant'; text: string; state?: 'streaming' | 'error' | 'cancelled'; meta?: Meta }
type VersionOption = { id: string; version: string; is_live: boolean }

let nextId = 1

/** Live test console (AST-054): the draft or a published version, streamed from a real run. */
export function TestConsole({ assistant, tenant }: { assistant: Assistant; tenant: TenantMembership }) {
  const { t } = useTranslation('testing')
  const tenantId = tenant.id
  const canRun = tenant.access_mode === 'full' && tenant.permissions.includes('assistants:test')
  const canAddCase = tenant.access_mode === 'full' && tenant.permissions.includes('assistants:edit') && assistant.can_edit

  const [versions, setVersions] = useState<VersionOption[]>([])
  const [versionId, setVersionId] = useState('draft')
  const [messages, setMessages] = useState<Message[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [runId, setRunId] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [addingCase, setAddingCase] = useState<string | null>(null)
  const abort = useRef<AbortController | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const sending = runId !== null

  useEffect(() => {
    let active = true
    apiRequest<{ items: VersionOption[] }>(`/api/v1/assistants/${assistant.id}/versions`, {}, tenantId)
      .then((result) => active && setVersions(result.items))
      .catch(() => active && setVersions([]))
    return () => { active = false; abort.current?.abort() }
  }, [assistant.id, assistant.live_version_id, tenantId])

  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }) }, [messages])

  const patchLast = (change: (message: Message) => Message) =>
    setMessages((current) => current.map((message, index) => (index === current.length - 1 ? change(message) : message)))

  function handleEvent(event: RunEvent) {
    if (event.type === 'token') patchLast((m) => ({ ...m, text: m.text + event.data.text }))
    else if (event.type === 'done') {
      const data = event.data
      patchLast((m) => ({
        ...m, text: m.text || data.text, state: undefined,
        meta: { latencyMs: data.latency_ms, tokensIn: data.usage?.input, tokensOut: data.usage?.output, traceUrl: data.trace_url, retrieval: data.retrieval?.mode, citations: data.retrieval?.citations },
      }))
    } else if (event.type === 'error') patchLast((m) => ({ ...m, state: 'error', text: event.data.message ?? t('console.failed') }))
    else if (event.type === 'cancelled') patchLast((m) => ({ ...m, state: 'cancelled' }))
  }

  async function send() {
    const message = input.trim()
    if (!message || sending || !canRun) return
    setNotice('')
    setInput('')
    setMessages((current) => [...current, { id: nextId++, role: 'user', text: message }, { id: nextId++, role: 'assistant', text: '', state: 'streaming' }])
    try {
      const started = await apiRequest<{ run_id: string; conversation_id: string }>(`/api/v1/assistants/${assistant.id}/runs`, {
        method: 'POST',
        body: JSON.stringify({ message, conversation_id: conversationId, version_id: versionId === 'draft' ? undefined : versionId }),
      }, tenantId)
      setConversationId(started.conversation_id)
      setRunId(started.run_id)
      abort.current = new AbortController()
      await streamRunEvents(started.run_id, tenantId, handleEvent, abort.current.signal)
    } catch (requestError) {
      if ((requestError as Error).name !== 'AbortError') {
        patchLast((m) => ({ ...m, state: 'error', text: requestError instanceof Error ? requestError.message : t('console.failed') }))
      }
    } finally {
      setRunId(null)
      // The stream ended without a terminal event (connection dropped): do not leave “thinking…” forever.
      setMessages((current) => current.map((message, index) => (
        index === current.length - 1 && message.state === 'streaming'
          ? { ...message, state: 'error', text: message.text || t('console.failed') }
          : message
      )))
    }
  }

  async function stop() {
    if (!runId) return
    try { await apiRequest(`/api/v1/runs/${runId}/cancel`, { method: 'POST' }, tenantId) } catch { /* already finished */ }
  }

  function reset() {
    abort.current?.abort()
    setMessages([])
    setConversationId(null)
    setRunId(null)
    setNotice('')
  }

  const greeting = assistant.config.greeting

  return (
    <aside aria-label={t('console.title')} className="test-console">
      <header>
        <h2><FlaskConical size={16} /> {t('console.title')}</h2>
        <button className="icon-button" disabled={messages.length === 0} onClick={reset} title={t('console.reset')} type="button"><RotateCcw size={15} /></button>
      </header>
      <div className="test-console-options">
        <label>{t('console.environment')}
          <select disabled title={t('console.environmentHint')} value="production"><option value="production">{t('console.production')}</option></select>
        </label>
        <label>{t('console.version')}
          <select disabled={sending} onChange={(event) => { setVersionId(event.target.value); reset() }} value={versionId}>
            <option value="draft">{t('console.draft')}</option>
            {versions.map((version) => <option key={version.id} value={version.id}>v{version.version}{version.is_live ? ` · ${t('console.live')}` : ''}</option>)}
          </select>
        </label>
      </div>
      <div className="test-console-messages" ref={scroller}>
        {messages.length === 0 && <p className="wizard-muted">{greeting || t('console.empty')}</p>}
        {messages.map((message) => (
          <div className={`console-message ${message.role}${message.state ? ` ${message.state}` : ''}`} key={message.id}>
            <p>{message.text || (message.state === 'streaming' ? t('console.thinking') : '')}{message.state === 'cancelled' && <em> {t('console.cancelled')}</em>}</p>
            {message.role === 'user' && canAddCase && <button className="text-button" onClick={() => setAddingCase(message.text)} type="button">{t('console.addCase')}</button>}
            {message.meta?.citations && message.meta.citations.length > 0 && (
              <div className="console-citations">
                <strong>{t('console.citations')}</strong>
                <ul>
                  {message.meta.citations.map((citation, index) => (
                    <li key={`${citation.document_name}:${index}`} title={citation.snippet}>
                      {citation.kind === 'faq' && <em>{t('console.faq')}</em>}
                      {citation.document_name}{citation.kb_name ? ` · ${citation.kb_name}` : ''}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {message.meta && (
              <small>
                {t('console.meta', { seconds: ((message.meta.latencyMs ?? 0) / 1000).toFixed(1), tokensIn: message.meta.tokensIn ?? '—', tokensOut: message.meta.tokensOut ?? '—' })}
                {message.meta.traceUrl && <> · <a href={message.meta.traceUrl} rel="noreferrer" target="_blank">{t('console.trace')} <ExternalLink size={11} /></a></>}
              </small>
            )}
          </div>
        ))}
      </div>
      {notice && <p className="assistants-notice" role="status">{notice}</p>}
      {!canRun && <p className="wizard-muted">{t('console.noPermission')}</p>}
      <div className="test-console-input">
        <textarea
          disabled={!canRun}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() } }}
          placeholder={t('console.placeholder')}
          rows={2}
          value={input}
        />
        {sending
          ? <button aria-label={t('console.stop')} className="secondary-button" onClick={() => void stop()} type="button"><Square size={14} /></button>
          : <button aria-label={t('console.send')} className="primary-button" disabled={!input.trim() || !canRun} onClick={() => void send()} type="button"><Send size={14} /></button>}
      </div>
      {addingCase !== null && (
        <TestCaseDialog
          assistantId={assistant.id}
          initial={{ input: addingCase }}
          onClose={() => setAddingCase(null)}
          onSaved={() => { setAddingCase(null); setNotice(t('console.caseAdded')) }}
          tenantId={tenantId}
        />
      )}
    </aside>
  )
}
