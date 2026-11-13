import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Plus,
  X,
} from 'lucide-react'
import { apiRequest } from '../../api'
import type { PlatformOperation, PlatformTenant } from '../../types'
import { enumLabel } from '../../i18n'

type TenantForm = {
  name: string
  slug: string
  adminName: string
  adminEmail: string
  adminPassword: string
  sendAdminInvitation: boolean
  seatLimit: number
  plan: string
  region: string
  industry: string
  provisioning: 'formal' | 'trial'
  trialDays: number
  trialEndsAt: string
  portalMode: 'named' | 'active' | 'unlimited'
  portalSeats: string
  portalOverage: 'hard' | 'grace_5' | 'grace_10'
}

type PortalDefaults = Record<string, Pick<TenantForm, 'portalMode' | 'portalSeats' | 'portalOverage'>>
type PlanRow = { code: string; latest_version: number; versions: { version: number; entitlements: Record<string, unknown>; quotas: Record<string, number | null> }[] }

/** The AI portal grant a plan hands out by default: read from the platform's plan definitions, never hard-coded here. */
function portalDefaultsOf(plans: PlanRow[]): PortalDefaults {
  const result: PortalDefaults = {}
  for (const plan of plans) {
    const version = plan.versions.find((item) => item.version === plan.latest_version)
    if (!version) continue
    result[plan.code] = {
      portalMode: (version.entitlements.portal_mode as TenantForm['portalMode']) ?? 'named',
      portalSeats: version.quotas.portal_seats == null ? '' : String(version.quotas.portal_seats),
      portalOverage: (version.entitlements.portal_overage as TenantForm['portalOverage']) ?? 'hard',
    }
  }
  return result
}

const PORTAL_MODES = [['named', '指定用户'], ['active', '按月活跃'], ['unlimited', '全员不限']] as const
const PORTAL_OVERAGES = [['hard', '硬限制'], ['grace_5', '宽限 5%'], ['grace_10', '宽限 10%']] as const

const EMPTY_FORM: TenantForm = {
  name: '', slug: '', adminName: '', adminEmail: '', adminPassword: '', sendAdminInvitation: false, seatLimit: 10, plan: 'standard',
  region: 'ap_singapore', industry: 'general_office', provisioning: 'formal', trialDays: 14, trialEndsAt: '',
  portalMode: 'named', portalSeats: '200', portalOverage: 'hard',
}

/** The tenant fields shared by the create form and the edit dialog (the edit dialog leaves the password optional). */
function TenantFields({ form, onChange, mode, status, memberCount, industries }: {
  industries: { code: string; name_zh: string }[]
  form: TenantForm
  onChange: (patch: Partial<TenantForm>) => void
  mode: 'create' | 'edit'
  status?: string
  memberCount?: number
}) {
  const editing = mode === 'edit'
  return (
    <>
      <label>企业名称<input maxLength={160} minLength={2} onChange={(event) => onChange({ name: event.target.value })} placeholder="例如：Acme 科技" required value={form.name} /></label>
      <label>工作区标识<input maxLength={80} minLength={3} onChange={(event) => onChange({ slug: event.target.value.toLowerCase() })} pattern="[a-z0-9][a-z0-9-]{1,78}[a-z0-9]" placeholder="例如：acme-tech" required value={form.slug} /><small>小写字母、数字和连字符；用于识别工作区。</small></label>
      <label>企业管理员姓名<input maxLength={120} onChange={(event) => onChange({ adminName: event.target.value })} placeholder="例如：Alex Chen" required={!editing} value={form.adminName} /></label>
      <label>企业管理员邮箱<input autoComplete="off" onChange={(event) => onChange({ adminEmail: event.target.value })} placeholder="admin@company.com" required={!editing} type="email" value={form.adminEmail} /></label>
      <label>{editing ? '重置管理员密码' : '初始登录密码'}<input autoComplete="new-password" minLength={12} disabled={form.sendAdminInvitation} onChange={(event) => onChange({ adminPassword: event.target.value })} required={!editing && !form.sendAdminInvitation} type="password" value={form.adminPassword} /><small>{editing ? '留空则不修改；设置后该管理员的现有登录会话会失效，请通过安全渠道交付新密码。' : form.sendAdminInvitation ? '已改为发送开通邀请邮件：管理员将自行设置密码。' : '至少 12 位，请通过安全渠道交付给企业管理员。'}</small></label>
      {!editing && <label className="checkbox-label"><input checked={form.sendAdminInvitation} onChange={(event) => onChange({ sendAdminInvitation: event.target.checked, adminPassword: event.target.checked ? '' : form.adminPassword })} type="checkbox" />立即发送开通邀请邮件<small>勾选后不设置初始密码：平台向管理员邮箱发送开通邀请（PLT-022），管理员通过邮件链接自行设置姓名与密码。可在「邮件投递」页查看发送状态。</small></label>}
      <label>{editing ? '席位上限' : '初始席位上限'}<input max={100000} min={1} onChange={(event) => onChange({ seatLimit: Number(event.target.value) })} required type="number" value={form.seatLimit} /><small>{editing ? `当前成员与待接受邀请共 ${memberCount ?? 0} 个；不能低于已使用的席位。` : '当前由平台按租户配置；后续套餐权益将接管此上限。'}</small></label>
      <label>套餐<select onChange={(event) => onChange({ plan: event.target.value })} value={form.plan}><option value="standard">标准版</option><option value="professional">专业版</option><option value="enterprise">企业版</option></select>{editing && <small>保存后立即按新套餐的权益生效。</small>}</label>
      <fieldset className="portal-grant-fields">
        <legend>AI 门户席位<small>员工使用 AI 门户的授权数量，与控制台席位分开计量；由租户管理员分配给员工。</small></legend>
        <label>计费模式<select onChange={(event) => onChange({ portalMode: event.target.value as TenantForm['portalMode'] })} value={form.portalMode}>{PORTAL_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {form.portalMode === 'unlimited' ? <small>全员不限：不设门户席位上限，也不适用超额策略。</small> : (
          <>
            <label>门户席位数<input min={1} onChange={(event) => onChange({ portalSeats: event.target.value })} required type="number" value={form.portalSeats} />{Number(form.portalSeats) <= 0 && <small className="portal-grant-error" role="alert">门户席位数需大于 0</small>}</label>
            <label>席位用尽时<select onChange={(event) => onChange({ portalOverage: event.target.value as TenantForm['portalOverage'] })} value={form.portalOverage}>{PORTAL_OVERAGES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </>
        )}
      </fieldset>
      <label>数据区域<select onChange={(event) => onChange({ region: event.target.value })} value={form.region}><option value="us_east">US-East</option><option value="eu_west">EU-West</option><option value="ap_singapore">AP-Singapore</option><option value="cn_shanghai">CN-Shanghai</option></select><small>模型目录仅显示本区域可用模型{editing ? '；修改区域会改变该企业可见的模型。' : '。'}</small></label>
      <label>所属行业<select onChange={(event) => onChange({ industry: event.target.value })} value={form.industry}>{industries.map((item) => <option key={item.code} value={item.code}>{item.name_zh}</option>)}</select><small>来自平台行业字典；决定可继承的行业术语表与按行业下发的安全基线。</small></label>
      {!editing && <label>开通方式<select onChange={(event) => onChange({ provisioning: event.target.value as 'formal' | 'trial' })} value={form.provisioning}><option value="formal">正式</option><option value="trial">试用</option></select><small>试用到期后自动停用。</small></label>}
      {!editing && form.provisioning === 'trial' && <label>试用天数<input max={90} min={1} onChange={(event) => onChange({ trialDays: Number(event.target.value) })} type="number" value={form.trialDays} /></label>}
      {editing && status === 'trial' && <label>试用到期日<input onChange={(event) => onChange({ trialEndsAt: event.target.value })} required type="date" value={form.trialEndsAt} /><small>到期后自动停用。</small></label>}
    </>
  )
}

export function PlatformTenantsPage({ currentUserId }: { currentUserId: string }) {
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [createForm, setCreateForm] = useState<TenantForm>(EMPTY_FORM)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [operations, setOperations] = useState<PlatformOperation[]>([])
  const [industries, setIndustries] = useState<{ code: string; name_zh: string }[]>([{ code: 'general_office', name_zh: '通用办公' }])
  const [lifecycleTenant, setLifecycleTenant] = useState<PlatformTenant | null>(null)
  const [lifecycleAction, setLifecycleAction] = useState('')
  const [lifecycleReason, setLifecycleReason] = useState('')
  const [lifecycleSubmitting, setLifecycleSubmitting] = useState(false)
  const [reviewingOperation, setReviewingOperation] = useState('')
  const [portalDefaults, setPortalDefaults] = useState<PortalDefaults>({})
  const [editing, setEditing] = useState<PlatformTenant | null>(null)
  const [editForm, setEditForm] = useState<TenantForm>(EMPTY_FORM)
  const [editSubmitting, setEditSubmitting] = useState(false)
  const [editError, setEditError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [resending, setResending] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
      apiRequest<PlatformOperation[]>('/api/v1/platform/operations?status=pending'),
      apiRequest<{ code: string; name_zh: string; status: string }[]>('/api/v1/platform/industries'),
      apiRequest<PlanRow[]>('/api/v1/platform/plans'),
    ])
      .then(([tenantResult, operationResult, industryResult, planResult]) => {
        if (!active) return
        const defaults = portalDefaultsOf(planResult)
        setPortalDefaults(defaults)
        setCreateForm((current) => ({ ...current, ...defaults[current.plan] }))
        setTenants(tenantResult)
        setOperations(operationResult)
        setIndustries(industryResult.filter((item) => item.status === 'active'))
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
            name: createForm.name,
            slug: createForm.slug,
            admin_name: createForm.adminName,
            admin_email: createForm.adminEmail,
            admin_password: createForm.sendAdminInvitation ? undefined : createForm.adminPassword,
            send_admin_invitation: createForm.sendAdminInvitation,
            seat_limit: createForm.seatLimit,
            plan: createForm.plan,
            region: createForm.region,
            industry: createForm.industry,
            provisioning: createForm.provisioning,
            trial_days: createForm.provisioning === 'trial' ? createForm.trialDays : undefined,
            portal_mode: createForm.portalMode,
            portal_seats: createForm.portalMode === 'unlimited' ? undefined : Number(createForm.portalSeats),
            portal_overage: createForm.portalMode === 'unlimited' ? undefined : createForm.portalOverage,
          }),
        },
      )
      setTenants((current) => [created, ...current])
      setCreateForm({ ...EMPTY_FORM, ...portalDefaults[EMPTY_FORM.plan] })
      setNotice(createForm.sendAdminInvitation
        ? `已创建 ${created.name}。开通邀请邮件已发送给 ${createForm.adminEmail}，管理员接受邀请后自行设置密码。`
        : `已创建 ${created.name}。首位企业管理员为 ${created.admin_email}；平台账号没有加入该企业。`)
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

  function openEdit(tenant: PlatformTenant) {
    setEditing(tenant)
    setEditForm({
      name: tenant.name, slug: tenant.slug, adminName: tenant.admin_name ?? '', adminEmail: tenant.admin_email ?? '',
      adminPassword: '', sendAdminInvitation: false, seatLimit: tenant.seat_limit, plan: tenant.plan, region: tenant.region, industry: tenant.industry, provisioning: 'formal',
      trialDays: 14, trialEndsAt: tenant.trial_ends_at ? tenant.trial_ends_at.slice(0, 10) : '',
      portalMode: tenant.portal_mode as TenantForm['portalMode'], portalSeats: tenant.portal_seats == null ? '' : String(tenant.portal_seats),
      portalOverage: (tenant.portal_overage ?? 'hard') as TenantForm['portalOverage'],
    })
    setEditError('')
  }

  async function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!editing) return
    setEditSubmitting(true)
    setEditError('')
    try {
      const body: Record<string, unknown> = {}
      if (editForm.name.trim() !== editing.name) body.name = editForm.name.trim()
      if (editForm.slug !== editing.slug) body.slug = editForm.slug
      if (editForm.plan !== editing.plan) body.plan = editForm.plan
      if (editForm.seatLimit !== editing.seat_limit) body.seat_limit = editForm.seatLimit
      if (
        editForm.portalMode !== editing.portal_mode
        || (editForm.portalMode !== 'unlimited' && (Number(editForm.portalSeats) !== editing.portal_seats || editForm.portalOverage !== editing.portal_overage))
      ) {
        body.portal_mode = editForm.portalMode
        if (editForm.portalMode !== 'unlimited') {
          body.portal_seats = Number(editForm.portalSeats)
          body.portal_overage = editForm.portalOverage
        }
      }
      if (editForm.region !== editing.region) body.region = editForm.region
      if (editForm.industry !== editing.industry) body.industry = editForm.industry
      if (editForm.adminName.trim() !== (editing.admin_name ?? '')) body.admin_name = editForm.adminName.trim()
      if (editForm.adminEmail.trim() !== (editing.admin_email ?? '')) body.admin_email = editForm.adminEmail.trim()
      if (editForm.adminPassword) body.admin_password = editForm.adminPassword
      if (editing.status === 'trial' && editForm.trialEndsAt && editForm.trialEndsAt !== (editing.trial_ends_at ?? '').slice(0, 10)) {
        body.trial_ends_at = new Date(`${editForm.trialEndsAt}T23:59:59`).toISOString()
      }
      if (Object.keys(body).length === 0) {
        setEditing(null)
        return
      }
      const updated = await apiRequest<PlatformTenant>(`/api/v1/platform/tenants/${editing.id}`, { method: 'PATCH', body: JSON.stringify(body) })
      setTenants((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setNotice(`已更新 ${updated.name}。`)
      setEditing(null)
    } catch (requestError) {
      setEditError(requestError instanceof Error ? requestError.message : '保存失败')
    } finally {
      setEditSubmitting(false)
    }
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

  async function resendAdminInvitation(tenant: PlatformTenant) {
    if (!tenant.admin_invitation_id) return
    setResending(tenant.id)
    setError('')
    setNotice('')
    try {
      const updated = await apiRequest<PlatformTenant>(
        `/api/v1/platform/tenants/${tenant.id}/admin-invitations/${tenant.admin_invitation_id}/resend`,
        { method: 'POST', body: JSON.stringify({}) },
      )
      setTenants((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setNotice(`已重新发送开通邀请到 ${tenant.admin_invitation_email}，旧链接已失效。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '重发开通邀请失败')
    } finally {
      setResending('')
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
          <TenantFields form={createForm} industries={industries} mode="create" onChange={(patch) => setCreateForm((current) => ({ ...current, ...patch, ...(patch.plan ? portalDefaults[patch.plan] : {}) }))} />
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
              <thead><tr><th>企业</th><th>首位企业管理员</th><th>套餐 / 区域</th><th>控制台席位</th><th>AI 门户席位</th><th>成员数</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{tenants.map((tenant) => (
                <tr key={tenant.id}>
                  <td><strong className="tenant-name-cell">{tenant.name}</strong><small className="platform-tenant-slug">{tenant.slug}</small></td>
                  <td>{tenant.admin_name ? <span className="platform-admin-cell"><strong>{tenant.admin_name}</strong><small>{tenant.admin_email}</small></span> : tenant.admin_invitation_id ? (
                    <span className="platform-admin-cell">
                      <strong>{tenant.admin_invitation_name ?? tenant.admin_invitation_email}</strong>
                      <small>{tenant.admin_invitation_email}</small>
                      <small className={tenant.admin_invitation_status === 'expired' ? 'form-error' : 'role-text'}>
                        {tenant.admin_invitation_status === 'expired' ? '邀请已过期' : '邀请待接受'}
                        {tenant.admin_invitation_expires_at ? `（${tenant.admin_invitation_status === 'expired' ? '过期于' : '有效至'} ${new Date(tenant.admin_invitation_expires_at).toLocaleDateString()}）` : ''}
                      </small>
                      {tenant.status !== 'deleted' && <button className="tenant-status-action" disabled={resending === tenant.id} onClick={() => void resendAdminInvitation(tenant)} type="button">{resending === tenant.id ? '发送中…' : '重发邀请'}</button>}
                    </span>
                  ) : <span className="role-text">未设置</span>}</td>
                  <td><span className="platform-admin-cell"><strong>{enumLabel('plan', tenant.plan)}</strong><small>{enumLabel('region', tenant.region)}</small></span></td>
                  <td>{tenant.seat_limit}</td>
                  <td><span className="platform-admin-cell"><strong>{tenant.portal_mode === 'unlimited' ? '全员不限' : (tenant.portal_seats ?? 0).toLocaleString()}</strong><small>{PORTAL_MODES.find(([value]) => value === tenant.portal_mode)?.[1]}</small></span></td>
                  <td>{tenant.member_count}</td>
                  <td><span className={`member-status ${['trial', 'active', 'grace'].includes(tenant.status) ? 'active' : 'inactive'}`}><i />{enumLabel('tenantStatus', tenant.status)}</span>{tenant.status === 'trial' && tenant.trial_ends_at && <small className="platform-tenant-slug">试用至 {new Date(tenant.trial_ends_at).toLocaleDateString()}</small>}{tenant.status_note && <small className="tenant-status-note">{tenant.status_note}</small>}</td>
                  <td className="member-actions-cell">
                    {tenant.status !== 'deleted' && <button className="tenant-status-action" onClick={() => openEdit(tenant)} type="button">编辑</button>}{' '}
                    {tenant.available_actions.length ? <button className="tenant-status-action" onClick={() => openLifecycle(tenant)} type="button">生命周期操作</button> : tenant.status === 'deleted' ? <span className="role-text">—</span> : null}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
      {editing && (
        <div className="modal-backdrop" role="presentation" onClick={() => !editSubmitting && setEditing(null)}>
          <form className="create-modal lifecycle-modal tenant-edit-modal" role="dialog" aria-modal="true" aria-labelledby="tenant-edit-title" onClick={(event) => event.stopPropagation()} onSubmit={submitEdit}>
            <button className="modal-close" type="button" aria-label="关闭" onClick={() => setEditing(null)}><X size={18} /></button>
            <div className="section-kicker">编辑企业</div>
            <h2 id="tenant-edit-title">{editing.name}</h2>
            <TenantFields form={editForm} industries={industries} memberCount={editing.member_count} mode="edit" onChange={(patch) => setEditForm((current) => ({ ...current, ...patch, ...(patch.plan ? portalDefaults[patch.plan] : {}) }))} status={editing.status} />
            {editError && <p className="members-error" role="alert">{editError}</p>}
            <button className="primary-button" disabled={editSubmitting} type="submit">{editSubmitting ? '保存中…' : '保存'}</button>
          </form>
        </div>
      )}
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
