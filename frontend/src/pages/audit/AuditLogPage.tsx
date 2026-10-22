import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Download,
  ShieldCheck,
} from 'lucide-react'
import { apiRequest } from '../../api'

export type AuditRecord = {
  id: string
  category: string
  result: 'success' | 'warning' | 'failure'
  created_at: string
  actor_name: string
  actor_email: string | null
  description: string
  source_ip: string | null
  details: Record<string, string>
}

export type AuditRecordsResponse = {
  events: AuditRecord[]
  total: number
  limit: number
  offset: number
}

export type AuditIntegrityResponse = {
  chain_scope: string
  is_valid: boolean
  checked_events: number
  unchained_legacy_events: number
  through_sequence: number
}

export type AuditFilters = {
  category: string
  search: string
  result: string
  start: string
  end: string
}

const emptyAuditFilters: AuditFilters = {
  category: '',
  search: '',
  result: '',
  start: '',
  end: '',
}

function auditQuery(filters: AuditFilters) {
  const params = new URLSearchParams()
  if (filters.category) params.set('category', filters.category)
  if (filters.search.trim()) params.set('search', filters.search.trim())
  if (filters.result) params.set('result', filters.result)
  if (filters.start) params.set('start', `${filters.start}T00:00:00Z`)
  if (filters.end) params.set('end', `${filters.end}T23:59:59Z`)
  return params
}

export function AuditLogPage({
  tenantId,
  canExport,
  platformMode = false,
}: {
  tenantId?: string
  canExport: boolean
  platformMode?: boolean
}) {
  const [filters, setFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [appliedFilters, setAppliedFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [offset, setOffset] = useState(0)
  const [requestVersion, setRequestVersion] = useState(0)
  const [data, setData] = useState<AuditRecordsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [integrity, setIntegrity] = useState<AuditIntegrityResponse | null>(null)
  const [integrityLoading, setIntegrityLoading] = useState(true)
  const [integrityError, setIntegrityError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const params = auditQuery(appliedFilters)
    params.set('limit', '50')
    params.set('offset', String(offset))
    const endpoint = platformMode ? '/api/v1/platform/audit/events' : '/api/v1/audit/events'
    apiRequest<AuditRecordsResponse>(`${endpoint}?${params}`, {}, tenantId)
      .then((response) => active && setData(response))
      .catch((reason: Error) => {
        if (!active) return
        setData(null)
        setError(reason.message)
      })
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [appliedFilters, offset, platformMode, requestVersion, tenantId])

  useEffect(() => {
    let active = true
    const endpoint = platformMode
      ? '/api/v1/platform/audit/integrity'
      : '/api/v1/audit/integrity'
    apiRequest<AuditIntegrityResponse>(endpoint, {}, tenantId)
      .then((response) => active && setIntegrity(response))
      .catch((reason: Error) => {
        if (!active) return
        setIntegrity(null)
        setIntegrityError(reason.message)
      })
      .finally(() => active && setIntegrityLoading(false))
    return () => {
      active = false
    }
  }, [platformMode, tenantId])

  function updateFilter(key: keyof AuditFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }))
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setLoading(true)
    setOffset(0)
    setAppliedFilters({ ...filters })
    setRequestVersion((version) => version + 1)
  }

  function goToOffset(nextOffset: number) {
    setError('')
    setLoading(true)
    setOffset(nextOffset)
  }

  async function exportCsv() {
    setExporting(true)
    setError('')
    try {
      const params = auditQuery(appliedFilters)
      const endpoint = platformMode
        ? '/api/v1/platform/audit/events/export'
        : '/api/v1/audit/events/export'
      const response = await fetch(`${endpoint}?${params}`, {
        credentials: 'same-origin',
        headers: tenantId ? { 'X-Tenant-ID': tenantId } : undefined,
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '导出审计日志失败')
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = platformMode ? 'beework-platform-audit.csv' : 'beework-audit.csv'
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导出审计日志失败')
    } finally {
      setExporting(false)
    }
  }

  const page = data ? Math.floor(offset / data.limit) + 1 : 1
  const pageCount = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1
  const pageSize = data?.limit ?? 50

  return (
    <section className="audit-page">
      <div className="welcome-row audit-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />{platformMode ? '平台治理 · 操作记录' : '安全与治理 · 操作记录'}</div>
          <h1>审计日志</h1>
          <p>{platformMode ? '平台级操作与企业业务审计分开存储和查询。' : '配置变更与安全事件按时间倒序记录。'}历史数据无法提供的来源 IP 会显示为「—」。</p>
        </div>
        {canExport && <button className="primary-button" type="button" disabled={exporting} onClick={exportCsv}><Download size={16} />{exporting ? '正在导出' : '导出 CSV'}</button>}
      </div>

      <div className={`audit-integrity${integrityError || (integrity && !integrity.is_valid) ? ' invalid' : ''}`} aria-live="polite">
        <ShieldCheck size={17} />
        <strong>哈希链完整性</strong>
        <span>
          {integrityLoading
            ? '正在校验…'
            : integrityError
              ? `校验失败：${integrityError}`
              : integrity?.is_valid
                ? `链上 ${integrity.checked_events} 条记录通过校验`
                : '检测到哈希链异常，请联系平台管理员'}
        </span>
        {integrity && integrity.unchained_legacy_events > 0 && (
          <small>{integrity.unchained_legacy_events} 条历史记录未纳入哈希链</small>
        )}
      </div>

      <form className="audit-filters" onSubmit={applyFilters}>
        <label>分类
          <select value={filters.category} onChange={(event) => updateFilter('category', event.target.value)}>
            <option value="">全部分类</option>
            <option value="assistants">助手</option>
            <option value="security">安全</option>
            <option value="members_permissions">成员与权限</option>
            <option value="billing">计费</option>
            <option value="developer">开发者</option>
          </select>
        </label>
        <label className="audit-search">操作人或操作内容
          <input value={filters.search} onChange={(event) => updateFilter('search', event.target.value)} placeholder="搜索操作人或操作内容…" />
        </label>
        <label>结果
          <select value={filters.result} onChange={(event) => updateFilter('result', event.target.value)}>
            <option value="">全部结果</option>
            <option value="success">成功</option>
            <option value="warning">告警</option>
            <option value="failure">失败或驳回</option>
          </select>
        </label>
        <label>开始日期<input type="date" value={filters.start} onChange={(event) => updateFilter('start', event.target.value)} /></label>
        <label>结束日期<input type="date" value={filters.end} onChange={(event) => updateFilter('end', event.target.value)} /></label>
        <button className="secondary-button" type="submit">筛选</button>
      </form>

      {error && <div className="members-error" role="alert">{error}</div>}
      <div className="audit-table-card">
        <div className="audit-table-meta"><strong>操作记录</strong><span>{data?.total ?? 0} 条</span></div>
        {loading ? <div className="audit-empty">正在加载审计记录…</div> : data?.events.length ? (
          <div className="audit-table-scroll">
            <table className="audit-table">
              <thead><tr><th>结果</th><th>时间</th><th>分类</th><th>操作人 / 操作描述</th><th>来源 IP</th><th>变更</th></tr></thead>
              <tbody>{data.events.map((entry) => (
                <tr key={entry.id}>
                  <td><span className={`audit-result ${entry.result}`}><i />{entry.result === 'success' ? '成功' : entry.result === 'warning' ? '告警' : '失败'}</span></td>
                  <td><time dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleString('zh-CN', { hour12: false })}</time></td>
                  <td><span className="audit-category">{({ assistants: '助手', security: '安全', members_permissions: '成员与权限', billing: '计费', developer: '开发者' } as Record<string, string>)[entry.category] ?? entry.category}</span></td>
                  <td><span className="audit-actor"><strong>{entry.actor_name}</strong><small>{entry.actor_email ?? '系统操作'} · {entry.description}</small></span></td>
                  <td>{entry.source_ip ?? '—'}</td>
                  <td><details className="audit-details"><summary>查看</summary><pre>{JSON.stringify(entry.details, null, 2)}</pre></details></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <div className="audit-empty">{error ? '审计记录加载失败' : '没有匹配的记录'}</div>}
        <div className="audit-pagination">
          <span>第 {page} / {pageCount} 页</span>
          <div>
            <button className="secondary-button" type="button" disabled={!data || offset === 0 || loading} onClick={() => goToOffset(Math.max(0, offset - pageSize))}>上一页</button>
            <button className="secondary-button" type="button" disabled={!data || offset + pageSize >= data.total || loading} onClick={() => goToOffset(offset + pageSize)}>下一页</button>
          </div>
        </div>
      </div>
    </section>
  )
}
