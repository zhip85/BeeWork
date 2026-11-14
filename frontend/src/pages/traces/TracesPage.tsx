import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Activity } from 'lucide-react'
import { apiRequest } from '../../api'

type Trace = {
  run_id: string
  trace_id: string
  assistant_id: string
  assistant_name: string
  conversation_id: string | null
  origin: 'online' | 'debug' | 'trial' | 'eval'
  source: string
  channel: string | null
  environment: string
  status: 'success' | 'error' | 'slow' | 'running'
  latency_ms: number | null
  tokens_in: number | null
  tokens_out: number | null
  error_code: string | null
  trace_available: boolean
  created_at: string
  path: 'A' | 'B' | null
  route_target: string | null
  handoff: string | null
  capability_error: string | null
  confidence: number | null
}
type ToolCall = { call_index: number; tool_name: string; source: string; status: string; error_code: string | null; attempts: number; breaker_state: string | null; duration_ms: number; attempt_ms: number[] }
type Cost = { key: string; runs: number; tokens_in: number; tokens_out: number; tool_calls: number; avg_latency_ms: number | null }
type Summary = {
  days: number; runs: number; path_a: number; path_b: number; avg_confidence: number | null
  handoff_reasons: { key: string; count: number }[]; failed_capabilities: { key: string; count: number }[]
  by_path: Cost[]; by_capability: Cost[]; truncated: boolean
}
type Span = {
  id: string
  parent_id: string | null
  name: string
  kind: string
  start_ms: number
  duration_ms: number | null
  status: 'ok' | 'error'
  message: string | null
  model: string | null
  tokens_in: number | null
  tokens_out: number | null
  cost: number | null
  input: string | null
  output: string | null
}
type Detail = Trace & {
  spans: Span[]
  spans_source: 'langfuse' | 'unavailable'
  notice: string | null
  timeline: { seq: number; type: string; offset_ms: number }[]
  route: { path: 'A' | 'B'; source?: string; target?: { name: string; kind?: string }; confidence?: number; second?: number; classifier_ms?: number; failure?: string; handoff?: string; capability_error?: string; candidates?: { name: string; score: number; confidence?: number }[] } | null
  tool_calls: ToolCall[]
}
type TracePage = { items: Trace[]; total: number; limit: number; offset: number }
type Filters = { assistant: string; channel: string; status: string; origin: string; from: string; to: string; minLatency: string; q: string; path: string; handoff: string; failed: boolean }

const EMPTY: Filters = { assistant: '', channel: '', status: '', origin: '', from: '', to: '', minLatency: '', q: '', path: '', handoff: '', failed: false }
const PAGE = 30
const HANDOFFS = ['no_candidates', 'classifier_failed', 'low_confidence', 'ambiguous', 'not_direct', 'write', 'slots_missing', 'slots_invalid', 'slots_failed', 'capability_failed', 'disabled']
const CHANNELS = ['website', 'api', 'wecom', 'h5', 'wechat']

function query(filters: Filters, offset: number) {
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) })
  if (filters.assistant) params.set('assistant_id', filters.assistant)
  if (filters.channel) params.set('channel', filters.channel)
  if (filters.status) params.set('status', filters.status)
  if (filters.origin) params.set('origin', filters.origin)
  if (filters.from) params.set('created_from', `${filters.from}T00:00:00Z`)
  if (filters.to) params.set('created_to', `${filters.to}T23:59:59Z`)
  if (filters.minLatency) params.set('min_latency_ms', String(Math.max(0, Number(filters.minLatency) * 1000)))
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.path) params.set('path', filters.path)
  if (filters.handoff) params.set('handoff', filters.handoff)
  if (filters.failed) params.set('capability_failed', 'true')
  return params
}

const seconds = (ms: number | null) => (ms === null ? '—' : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`)

function Waterfall({ spans }: { spans: Span[] }) {
  const { t } = useTranslation('traces')
  const [open, setOpen] = useState<string | null>(null)
  const total = useMemo(() => Math.max(1, ...spans.map((span) => span.start_ms + (span.duration_ms ?? 0))), [spans])
  const depth = useMemo(() => {
    const parents = new Map(spans.map((span) => [span.id, span.parent_id]))
    const levels = new Map<string, number>()
    const level = (id: string): number => {
      if (levels.has(id)) return levels.get(id) ?? 0
      const parent = parents.get(id)
      const value = parent && parents.has(parent) ? level(parent) + 1 : 0
      levels.set(id, value)
      return value
    }
    spans.forEach((span) => level(span.id))
    return levels
  }, [spans])
  return (
    <ol className="trace-waterfall">
      {spans.map((span) => (
        <li key={span.id}>
          <button className="trace-span" type="button" onClick={() => setOpen(open === span.id ? null : span.id)} aria-expanded={open === span.id}>
            <span className="trace-span-name" style={{ paddingLeft: `${(depth.get(span.id) ?? 0) * 14}px` }}>
              <i className={`trace-kind ${span.kind}`} />{span.name}
            </span>
            <span className="trace-span-bar">
              <i className={span.status === 'error' ? 'error' : ''} style={{ left: `${(span.start_ms / total) * 100}%`, width: `${Math.max(0.6, ((span.duration_ms ?? 0) / total) * 100)}%` }} />
            </span>
            <span className="trace-span-time">{seconds(span.duration_ms)}</span>
          </button>
          {open === span.id && (
            <dl className="trace-span-detail">
              <dt>{t('detail.start')}</dt><dd>+{span.start_ms} ms</dd>
              {span.model && <><dt>{t('detail.model')}</dt><dd>{span.model}</dd></>}
              {(span.tokens_in !== null || span.tokens_out !== null) && <><dt>{t('detail.tokens')}</dt><dd>{span.tokens_in ?? 0} / {span.tokens_out ?? 0}</dd></>}
              {span.cost !== null && <><dt>{t('detail.cost')}</dt><dd>{span.cost}</dd></>}
              {span.message && <><dt>{t('detail.error')}</dt><dd>{span.message}</dd></>}
              {span.input && <><dt>{t('detail.input')}</dt><dd><pre>{span.input}</pre></dd></>}
              {span.output && <><dt>{t('detail.output')}</dt><dd><pre>{span.output}</pre></dd></>}
            </dl>
          )}
        </li>
      ))}
    </ol>
  )
}

function RouteOverview({ tenantId, assistant }: { tenantId: string; assistant: string }) {
  const { t } = useTranslation('traces')
  const [summary, setSummary] = useState<Summary | null>(null)
  useEffect(() => {
    let active = true
    const params = new URLSearchParams({ days: '7' })
    if (assistant) params.set('assistant_id', assistant)
    apiRequest<Summary>(`/api/v1/traces/route-summary?${params}`, {}, tenantId)
      .then((value) => active && setSummary(value))
      .catch(() => active && setSummary(null))
    return () => {
      active = false
    }
  }, [tenantId, assistant])
  if (!summary || summary.runs === 0) return null
  const share = (count: number) => `${Math.round((count / summary.runs) * 100)}%`
  return (
    <section className="trace-overview" aria-label={t('overview.title')}>
      <h2>{t('overview.title')}</h2>
      <div className="trace-overview-grid">
        <div><span>{t('overview.pathA')}</span><strong>{share(summary.path_a)}</strong><small>{summary.path_a}</small></div>
        <div><span>{t('overview.pathB')}</span><strong>{share(summary.path_b)}</strong><small>{summary.path_b}</small></div>
        <div><span>{t('overview.confidence')}</span><strong>{summary.avg_confidence === null ? '—' : summary.avg_confidence.toFixed(2)}</strong></div>
        <div>
          <span>{t('overview.handoffs')}</span>
          {summary.handoff_reasons.length ? <ul>{summary.handoff_reasons.slice(0, 3).map((item) => <li key={item.key}>{t(`handoff.${item.key}`, item.key)} · {item.count}</li>)}</ul> : <strong>—</strong>}
        </div>
        <div>
          <span>{t('overview.failed')}</span>
          {summary.failed_capabilities.length ? <ul>{summary.failed_capabilities.slice(0, 3).map((item) => <li key={item.key}>{item.key} · {item.count}</li>)}</ul> : <strong>—</strong>}
        </div>
      </div>
      <table className="audit-table trace-costs">
        <thead><tr><th>{t('overview.cost')}</th><th>{t('overview.runs')}</th><th>{t('columns.tokens')}</th><th>{t('overview.toolCalls')}</th><th>{t('columns.latency')}</th></tr></thead>
        <tbody>
          {summary.by_path.map((row) => <tr key={`p-${row.key}`}><td>{t(`path.${row.key}`)}</td><td>{row.runs}</td><td>{row.tokens_in} / {row.tokens_out}</td><td>{row.tool_calls}</td><td>{seconds(row.avg_latency_ms)}</td></tr>)}
          {summary.by_capability.slice(0, 5).map((row) => <tr key={`c-${row.key}`}><td>{row.key}</td><td>{row.runs}</td><td>{row.tokens_in} / {row.tokens_out}</td><td>{row.tool_calls}</td><td>{seconds(row.avg_latency_ms)}</td></tr>)}
        </tbody>
      </table>
      {summary.truncated && <small>{t('overview.truncated')}</small>}
    </section>
  )
}

function RouteDetail({ detail }: { detail: Detail }) {
  const { t } = useTranslation('traces')
  const route = detail.route
  if (!route && detail.tool_calls.length === 0) return null
  return (
    <section className="trace-route">
      {route && (
        <>
          <h3>{t('route.title')}</h3>
          <p className="trace-ids">
            <span>{t(`path.${route.path}`)}</span>
            {route.target && <span>{route.target.name}</span>}
            {route.source && <span>{t(`route.source.${route.source}`, route.source)}</span>}
            {route.confidence !== undefined && <span>{t('route.confidence')} {route.confidence}</span>}
            {route.classifier_ms !== undefined && <span>{t('route.classifier')} {route.classifier_ms} ms</span>}
            {route.handoff && <span>{t('route.handoff')}：{t(`handoff.${route.handoff}`, route.handoff)}</span>}
            {route.capability_error && <span>{t('route.failedWith')} {route.capability_error}</span>}
          </p>
          {route.candidates && route.candidates.length > 0 && (
            <ul className="trace-timeline">{route.candidates.map((item) => <li key={item.name}>{item.name} · {t('route.recall')} {item.score}{item.confidence !== undefined ? ` · ${t('route.confidence')} ${item.confidence}` : ''}</li>)}</ul>
          )}
        </>
      )}
      {detail.tool_calls.length > 0 && (
        <>
          <h3>{t('route.calls')}</h3>
          <table className="audit-table">
            <thead><tr><th>#</th><th>{t('route.tool')}</th><th>{t('columns.status')}</th><th>{t('route.attempts')}</th><th>{t('columns.latency')}</th></tr></thead>
            <tbody>{detail.tool_calls.map((call) => (
              <tr key={`${call.call_index}-${call.tool_name}`}>
                <td>{call.call_index}</td><td>{call.tool_name}</td>
                <td>{call.status}{call.error_code ? ` · ${call.error_code}` : ''}{call.breaker_state && call.breaker_state !== 'closed' ? ` · ${t('route.circuit')} ${call.breaker_state}` : ''}</td>
                <td>{call.attempts}</td><td>{seconds(call.duration_ms)}</td>
              </tr>
            ))}</tbody>
          </table>
        </>
      )}
    </section>
  )
}

function TraceDetail({ runId, tenantId, onClose }: { runId: string; tenantId: string; onClose: () => void }) {
  const { t } = useTranslation('traces')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    apiRequest<Detail>(`/api/v1/traces/${runId}`, {}, tenantId)
      .then((value) => active && setDetail(value))
      .catch((reason: Error) => active && setError(reason.message))
    return () => {
      active = false
    }
  }, [runId, tenantId])
  return (
    <aside className="trace-drawer" role="dialog" aria-label={t('detail.title')}>
      <header>
        <h2>{t('detail.title')}</h2>
        <button className="secondary-button" type="button" onClick={onClose}>{t('detail.close')}</button>
      </header>
      {error && <div className="members-error" role="alert">{error}</div>}
      {!detail && !error && <div className="audit-empty">{t('loading')}</div>}
      {detail && (
        <div className="trace-drawer-body">
          <p className="trace-ids"><span>{t('columns.traceId')}</span><code>{detail.trace_id}</code></p>
          <p className="trace-ids">
            <span>{detail.assistant_name}</span>
            <span>{t(`status.${detail.status}`)}</span>
            <span>{seconds(detail.latency_ms)}</span>
            <span>{detail.tokens_in ?? 0} / {detail.tokens_out ?? 0} tokens</span>
            {detail.conversation_id && <Link to={`/assistants/${detail.assistant_id}?tab=conversations&conversation=${detail.conversation_id}`}>{t('detail.conversation')}</Link>}
          </p>
          <RouteDetail detail={detail} />
          {detail.notice && <div className="trace-notice" role="status">{detail.notice}</div>}
          {detail.spans.length > 0 && <Waterfall spans={detail.spans} />}
          {detail.spans_source === 'unavailable' && detail.timeline.length > 0 && (
            <>
              <h3>{t('detail.timeline')}</h3>
              <ul className="trace-timeline">{detail.timeline.map((event) => <li key={event.seq}><code>+{event.offset_ms} ms</code> {event.type}</li>)}</ul>
            </>
          )}
        </div>
      )}
    </aside>
  )
}

export function TracesPage({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation('traces')
  const [filters, setFilters] = useState<Filters>(EMPTY)
  const [applied, setApplied] = useState<Filters>(EMPTY)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<TracePage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [assistants, setAssistants] = useState<{ id: string; name: string }[]>([])
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    apiRequest<{ items: { id: string; name: string }[] }>('/api/v1/assistants?limit=100&sort=name_asc', {}, tenantId)
      .then((value) => setAssistants(value.items))
      .catch(() => setAssistants([]))
  }, [tenantId])

  useEffect(() => {
    let active = true
    apiRequest<TracePage>(`/api/v1/traces?${query(applied, offset)}`, {}, tenantId)
      .then((value) => {
        if (!active) return
        setData(value)
        setError('')
      })
      .catch((reason: Error) => {
        if (!active) return
        setData(null)
        setError(reason.message)
      })
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [applied, offset, tenantId])

  const update = (key: keyof Filters, value: string) => setFilters((current) => ({ ...current, [key]: value }))
  function apply(event: FormEvent) {
    event.preventDefault()
    setLoading(true)
    setOffset(0)
    setApplied(filters)
  }
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1

  return (
    <section className="audit-page traces-page">
      <div className="welcome-row audit-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />{t('eyebrow')}</div>
          <h1>{t('title')}</h1>
          <p>{t('subtitle')}</p>
        </div>
      </div>
      <form className="audit-filters" onSubmit={apply}>
        <label className="audit-search">{t('filters.search')}
          <input value={filters.q} onChange={(event) => update('q', event.target.value)} placeholder={t('filters.searchHint')} />
        </label>
        <label>{t('filters.assistant')}
          <select value={filters.assistant} onChange={(event) => update('assistant', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {assistants.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>{t('filters.channel')}
          <select value={filters.channel} onChange={(event) => update('channel', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {CHANNELS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>{t('filters.status')}
          <select value={filters.status} onChange={(event) => update('status', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {['success', 'error', 'slow'].map((item) => <option key={item} value={item}>{t(`status.${item}`)}</option>)}
          </select>
        </label>
        <label>{t('filters.origin')}
          <select value={filters.origin} onChange={(event) => update('origin', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {['online', 'debug', 'trial', 'eval'].map((item) => <option key={item} value={item}>{t(`origin.${item}`)}</option>)}
          </select>
        </label>
        <label>{t('filters.path')}
          <select value={filters.path} onChange={(event) => update('path', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {['A', 'B'].map((item) => <option key={item} value={item}>{t(`path.${item}`)}</option>)}
          </select>
        </label>
        <label>{t('filters.handoff')}
          <select value={filters.handoff} onChange={(event) => update('handoff', event.target.value)}>
            <option value="">{t('filters.all')}</option>
            {HANDOFFS.map((item) => <option key={item} value={item}>{t(`handoff.${item}`)}</option>)}
          </select>
        </label>
        <label className="trace-check"><input type="checkbox" checked={filters.failed} onChange={(event) => setFilters((current) => ({ ...current, failed: event.target.checked }))} />{t('filters.failed')}</label>
        <label>{t('filters.minLatency')}<input min="0" step="0.5" type="number" value={filters.minLatency} onChange={(event) => update('minLatency', event.target.value)} /></label>
        <label>{t('filters.from')}<input type="date" value={filters.from} onChange={(event) => update('from', event.target.value)} /></label>
        <label>{t('filters.to')}<input type="date" value={filters.to} onChange={(event) => update('to', event.target.value)} /></label>
        <button className="secondary-button" type="submit">{t('filters.apply')}</button>
      </form>
      {error && <div className="members-error" role="alert">{error}</div>}
      <RouteOverview tenantId={tenantId} assistant={applied.assistant} />
      <div className="audit-table-card">
        <div className="audit-table-meta"><strong>{t('list')}</strong><span>{t('count', { count: data?.total ?? 0 })}</span></div>
        {loading ? <div className="audit-empty">{t('loading')}</div> : data?.items.length ? (
          <div className="audit-table-scroll">
            <table className="audit-table">
              <thead><tr>
                <th>{t('columns.status')}</th><th>{t('columns.time')}</th><th>{t('columns.assistant')}</th><th>{t('columns.origin')}</th><th>{t('columns.route')}</th>
                <th>{t('columns.latency')}</th><th>{t('columns.tokens')}</th><th>{t('columns.traceId')}</th>
              </tr></thead>
              <tbody>{data.items.map((item) => (
                <tr className="trace-row" key={item.run_id} onClick={() => setSelected(item.run_id)} tabIndex={0} onKeyDown={(event) => event.key === 'Enter' && setSelected(item.run_id)}>
                  <td><span className={`trace-status ${item.status}`}><i />{t(`status.${item.status}`)}</span></td>
                  <td><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString(undefined, { hour12: false })}</time></td>
                  <td>{item.assistant_name}</td>
                  <td>{t(`origin.${item.origin}`)}{item.channel ? ` · ${item.channel}` : ''}</td>
                  <td>{item.path ? `${item.path}${item.route_target ? ` · ${item.route_target}` : ''}${item.handoff ? ` · ${t(`handoff.${item.handoff}`, item.handoff)}` : ''}` : '—'}</td>
                  <td>{seconds(item.latency_ms)}</td>
                  <td>{item.tokens_in ?? 0} / {item.tokens_out ?? 0}</td>
                  <td><code>{item.trace_id.slice(0, 12)}…</code>{!item.trace_available && <small className="trace-expired"> {t('expired')}</small>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <div className="audit-empty"><Activity size={18} /> {error ? t('failed') : t('empty')}</div>}
        <div className="audit-pagination">
          <span>{t('page', { page: Math.floor(offset / PAGE) + 1, pages })}</span>
          <div>
            <button className="secondary-button" type="button" disabled={offset === 0 || loading} onClick={() => { setLoading(true); setOffset(Math.max(0, offset - PAGE)) }}>{t('prev')}</button>
            <button className="secondary-button" type="button" disabled={!data || offset + PAGE >= data.total || loading} onClick={() => { setLoading(true); setOffset(offset + PAGE) }}>{t('next')}</button>
          </div>
        </div>
      </div>
      {selected && <TraceDetail key={selected} runId={selected} tenantId={tenantId} onClose={() => setSelected(null)} />}
    </section>
  )
}
