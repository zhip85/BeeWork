import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Plus,
  X,
} from 'lucide-react'
import { apiRequest } from '../../api'
import type { PlatformOperation, PlatformTenant } from '../../types'
import { enumLabel } from '../../i18n'

export function PlatformTenantsPage({ currentUserId }: { currentUserId: string }) {
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [adminName, setAdminName] = useState('')
  const [adminEmail, setAdminEmail] = useState('')
  const [adminPassword, setAdminPassword] = useState('')
  const [seatLimit, setSeatLimit] = useState(10)
  const [plan, setPlan] = useState('standard')
  const [region, setRegion] = useState('ap_singapore')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [operations, setOperations] = useState<PlatformOperation[]>([])
  const [lifecycleTenant, setLifecycleTenant] = useState<PlatformTenant | null>(null)
  const [lifecycleAction, setLifecycleAction] = useState('')
  const [lifecycleReason, setLifecycleReason] = useState('')
  const [lifecycleSubmitting, setLifecycleSubmitting] = useState(false)
  const [reviewingOperation, setReviewingOperation] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
      apiRequest<PlatformOperation[]>('/api/v1/platform/operations?status=pending'),
    ])
      .then(([tenantResult, operationResult]) => {
        if (!active) return
        setTenants(tenantResult)
        setOperations(operationResult)
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  async function createTenant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    setNotice('')
    try {
      const created = await apiRequest<PlatformTenant>(
        '/api/v1/platform/tenants',
        {
          method: 'POST',
          body: JSON.stringify({
            name,
            slug,
            admin_name: adminName,
            admin_email: adminEmail,
            admin_password: adminPassword,
            seat_limit: seatLimit,
            plan,
            region,
          }),
        },
      )
      setTenants((current) => [created, ...current])
      setName('')
      setSlug('')
      setAdminName('')
      setAdminEmail('')
      setAdminPassword('')
      setSeatLimit(10)
      setPlan('standard')
      setRegion('ap_singapore')
      setNotice(`已创建 ${created.name}。首位企业管理员为 ${created.admin_email}；平台账号没有加入该企业。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建企业失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function refresh() {
    const [tenantResult, operationResult] = await Promise.all([
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
      apiRequest<PlatformOperation[]>('/api/v1/platform/operations?status=pending'),
    ])
    setTenants(tenantResult)
    setOperations(operationResult)
  }

  function openLifecycle(tenant: PlatformTenant) {
    setLifecycleTenant(tenant)
    setLifecycleAction(tenant.available_actions[0]?.action ?? '')
    setLifecycleReason('')
  }

  const selectedAction = lifecycleTenant?.available_actions.find((item) => item.action === lifecycleAction)

  async function submitLifecycle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!lifecycleTenant || !selectedAction) return
    setLifecycleSubmitting(true)
    setError('')
    setNotice('')
    try {
      const result = await apiRequest<{ tenant: PlatformTenant; operation: PlatformOperation | null }>(
        `/api/v1/platform/tenants/${lifecycleTenant.id}/lifecycle`,
        { method: 'POST', body: JSON.stringify({ action: selectedAction.action, reason: lifecycleReason }) },
      )
      await refresh()
      setNotice(result.operation ? '操作已记录，等待第二名平台管理员复核。' : `${result.tenant.name} 已更新为「${enumLabel('tenantStatus', result.tenant.status)}」。`)
      setLifecycleTenant(null)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '生命周期操作失败')
    } finally {
      setLifecycleSubmitting(false)
    }
  }

  async function reviewOperation(operation: PlatformOperation, decision: 'approve' | 'reject') {
    setReviewingOperation(operation.id)
    setError('')
    setNotice('')
    try {
      await apiRequest<PlatformOperation>(
        `/api/v1/platform/operations/${operation.id}/${decision}`,
        { method: 'POST', body: JSON.stringify({}) },
      )
      await refresh()
      setNotice(decision === 'approve' ? `已复核通过：${operation.target_tenant_name} ${operation.operation_label}。` : `已驳回：${operation.target_tenant_name} ${operation.operation_label}。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '复核失败')
    } finally {
      setReviewingOperation('')
    }
  }

  return (
    <section className="platform-tenants-page">
      <div className="platform-page-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />运营数据 · 租户</div>
          <h1>租户管理</h1>
          <p>开通企业工作区、设置首位企业管理员，并管理租户服务状态。</p>
        </div>
        <div className="members-count"><strong>{tenants.length}</strong><span>家企业</span></div>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <div className="panel tenant-create-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">租户开通</div><h2>创建企业工作区</h2></div>
          <span className="tenant-role-chip">不授予平台账号企业权限</span>
        </div>
        <p className="organization-description">初始密码由平台管理员设置，创建后不会再次显示；请先记录并通过安全渠道交付。</p>
        <form className="tenant-create-form" onSubmit={createTenant}>
          <label>企业名称<input maxLength={160} minLength={2} onChange={(event) => setName(event.target.value)} placeholder="例如：Acme 科技" required value={name} /></label>
          <label>工作区标识<input maxLength={80} minLength={3} onChange={(event) => setSlug(event.target.value.toLowerCase())} pattern="[a-z0-9][a-z0-9-]{1,78}[a-z0-9]" placeholder="例如：acme-tech" required value={slug} /><small>小写字母、数字和连字符；创建后用于识别工作区。</small></label>
          <label>企业管理员姓名<input maxLength={120} onChange={(event) => setAdminName(event.target.value)} placeholder="例如：Alex Chen" required value={adminName} /></label>
          <label>企业管理员邮箱<input autoComplete="off" onChange={(event) => setAdminEmail(event.target.value)} placeholder="admin@company.com" required type="email" value={adminEmail} /></label>
          <label>初始登录密码<input autoComplete="new-password" minLength={12} onChange={(event) => setAdminPassword(event.target.value)} required type="password" value={adminPassword} /><small>至少 12 位，请通过安全渠道交付给企业管理员。</small></label>
          <label>初始席位上限<input min={1} max={100000} onChange={(event) => setSeatLimit(Number(event.target.value))} required type="number" value={seatLimit} /><small>当前由平台按租户配置；后续套餐权益将接管此上限。</small></label>
          <label>套餐<select onChange={(event) => setPlan(event.target.value)} value={plan}><option value="standard">标准版</option><option value="professional">专业版</option><option value="enterprise">企业版</option></select></label>
          <label>数据区域<select onChange={(event) => setRegion(event.target.value)} value={region}><option value="us_east">US-East</option><option value="eu_west">EU-West</option><option value="ap_singapore">AP-Singapore</option><option value="cn_shanghai">CN-Shanghai</option></select><small>模型目录仅显示本区域可用模型。</small></label>
          <button className="primary-button tenant-provision-submit" disabled={submitting} type="submit"><Plus size={16} />{submitting ? '正在创建…' : '创建企业并开通管理员'}</button>
        </form>
      </div>
      {operations.length > 0 && (
        <div className="panel tenants-list-panel">
          <div className="members-table-heading">
            <div><div className="section-kicker">双人复核</div><h2>待复核的高危操作</h2></div>
            <span className="tenant-role-chip">复核人不能是发起人 · 72 小时未处理自动驳回</span>
          </div>
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>企业</th><th>操作</th><th>原因</th><th>发起人</th><th>截止</th><th>复核</th></tr></thead>
              <tbody>{operations.map((operation) => {
                const ownRequest = operation.requested_by_id === currentUserId
                return (
                  <tr key={operation.id}>
                    <td><strong className="tenant-name-cell">{operation.target_tenant_name}</strong></td>
                    <td>{operation.operation_label}<small className="platform-tenant-slug">{enumLabel('tenantStatus', operation.from_status)} → {enumLabel('tenantStatus', operation.to_status)}</small></td>
                    <td>{operation.reason}</td>
                    <td>{operation.requested_by_name}</td>
                    <td>{new Date(operation.expires_at).toLocaleString('zh-CN')}</td>
                    <td className="member-actions-cell">
                      {ownRequest ? <span className="role-text">等待他人复核</span> : (
                        <>
                          <button className="tenant-status-action" disabled={reviewingOperation === operation.id} onClick={() => void reviewOperation(operation, 'approve')} type="button">通过并执行</button>{' '}
                          <button className="tenant-status-action suspend" disabled={reviewingOperation === operation.id} onClick={() => void reviewOperation(operation, 'reject')} type="button">驳回</button>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}</tbody>
            </table>
          </div>
        </div>
      )}
      <div className="panel tenants-list-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">租户生命周期</div><h2>所有企业</h2></div>
        </div>
        {loading ? <div className="members-empty">正在读取企业列表…</div> : tenants.length === 0 ? <div className="members-empty">还没有企业工作区。</div> : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>企业</th><th>首位企业管理员</th><th>套餐 / 区域</th><th>席位上限</th><th>成员数</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{tenants.map((tenant) => (
                <tr key={tenant.id}>
                  <td><strong className="tenant-name-cell">{tenant.name}</strong><small className="platform-tenant-slug">{tenant.slug}</small></td>
                  <td>{tenant.admin_name ? <span className="platform-admin-cell"><strong>{tenant.admin_name}</strong><small>{tenant.admin_email}</small></span> : <span className="role-text">未设置</span>}</td>
                  <td><span className="platform-admin-cell"><strong>{enumLabel('plan', tenant.plan)}</strong><small>{enumLabel('region', tenant.region)}</small></span></td>
                  <td>{tenant.seat_limit}</td>
                  <td>{tenant.member_count}</td>
                  <td><span className={`member-status ${['trial', 'active', 'grace'].includes(tenant.status) ? 'active' : 'inactive'}`}><i />{enumLabel('tenantStatus', tenant.status)}</span></td>
                  <td>{tenant.available_actions.length ? <button className="tenant-status-action" onClick={() => openLifecycle(tenant)} type="button">生命周期操作</button> : <span className="role-text">—</span>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
      {lifecycleTenant && (
        <div className="modal-backdrop" role="presentation" onClick={() => !lifecycleSubmitting && setLifecycleTenant(null)}>
          <form className="create-modal lifecycle-modal" role="dialog" aria-modal="true" aria-labelledby="lifecycle-title" onClick={(event) => event.stopPropagation()} onSubmit={submitLifecycle}>
            <button className="modal-close" type="button" aria-label="关闭" onClick={() => setLifecycleTenant(null)}><X size={18} /></button>
            <div className="section-kicker">生命周期操作</div>
            <h2 id="lifecycle-title">{selectedAction?.label ?? '选择操作'}</h2>
            <label>操作
              <select onChange={(event) => setLifecycleAction(event.target.value)} value={lifecycleAction}>
                {lifecycleTenant.available_actions.map((item) => <option key={item.action} value={item.action}>{item.label}</option>)}
              </select>
            </label>
            <p>{lifecycleTenant.name}：{enumLabel('tenantStatus', lifecycleTenant.status)} → {selectedAction ? enumLabel('tenantStatus', selectedAction.to_status) : '—'}。{selectedAction?.requires_review && '停用与删除需双人复核，并全程留痕。'}</p>
            <label>原因{selectedAction?.requires_review && <span className="required-mark"> *</span>}
              <textarea maxLength={500} onChange={(event) => setLifecycleReason(event.target.value)} required={selectedAction?.requires_review} rows={3} value={lifecycleReason} />
            </label>
            <button className={`primary-button${selectedAction?.requires_review ? ' danger-button' : ''}`} disabled={lifecycleSubmitting || !selectedAction || (selectedAction.requires_review && !lifecycleReason.trim())} type="submit">
              {lifecycleSubmitting ? '提交中…' : selectedAction?.requires_review ? '提交复核' : '确认'}
            </button>
          </form>
        </div>
      )}
    </section>
  )
}
