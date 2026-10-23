import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { apiRequest } from '../../api'
import type { PlatformJob } from '../../types'

type JobPage = { items: PlatformJob[]; total: number }

export function PlatformJobsPage() {
  const [jobs, setJobs] = useState<PlatformJob[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const result = await apiRequest<JobPage>('/api/v1/platform/jobs?limit=100')
      setJobs(result.items)
      setError('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '无法加载任务')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let active = true
    apiRequest<JobPage>('/api/v1/platform/jobs?limit=100')
      .then((result) => active && setJobs(result.items))
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [])

  const retry = async (job: PlatformJob) => {
    await apiRequest(`/api/v1/platform/jobs/${job.id}/retry`, { method: 'POST' })
    await load()
  }

  return <section className="settings-page">
    <div className="page-heading"><div><p className="section-kicker">运维</p><h1>后台任务</h1><p>查看 PostgreSQL 队列中的任务、进度和失败原因。</p></div><button className="secondary-button" type="button" onClick={() => void load()}><RefreshCw size={15} />刷新</button></div>
    {loading ? <p>正在加载任务…</p> : error ? <p className="form-error">{error}</p> : <div className="table-wrap"><table><thead><tr><th>类型</th><th>状态</th><th>尝试</th><th>计划时间</th><th>失败原因</th><th /></tr></thead><tbody>{jobs.length === 0 ? <tr><td colSpan={6}>暂无任务</td></tr> : jobs.map((job) => <tr key={job.id}><td>{job.kind}</td><td>{job.status}</td><td>{job.attempts}/{job.max_attempts}</td><td>{new Date(job.run_after).toLocaleString()}</td><td>{job.last_error ?? '—'}</td><td>{['dead', 'failed', 'succeeded'].includes(job.status) && <button className="text-button" type="button" onClick={() => void retry(job)}>重试</button>}</td></tr>)}</tbody></table></div>}
  </section>
}
