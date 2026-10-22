import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Building2,
  Plus,
  Pencil,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { apiRequest } from '../../api'
import { RoleManagementPanel } from './RoleManagementPanel'
import { roleLabel } from '../../roles'
import type { CreatedMemberInvitation, MemberInvitation, Organization, PermissionCatalog, TenantMember, TenantMembership, TenantRole } from '../../types'

export function MembersPage({
  tenant,
  currentUserId,
  onMembershipChanged,
}: {
  tenant: TenantMembership
  currentUserId: string
  onMembershipChanged: () => Promise<void>
}) {
  const [members, setMembers] = useState<TenantMember[]>([])
  const [invitations, setInvitations] = useState<MemberInvitation[]>([])
  const [seatLimit, setSeatLimit] = useState(0)
  const [seatsUsed, setSeatsUsed] = useState(0)
  const [inviteEmails, setInviteEmails] = useState('')
  const [inviteRole, setInviteRole] = useState('builder')
  const [inviteOrganizationId, setInviteOrganizationId] = useState('')
  const [inviting, setInviting] = useState(false)
  const [oneTimeLinks, setOneTimeLinks] = useState<Record<string, string>>({})
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [tenantRoles, setTenantRoles] = useState<TenantRole[]>([])
  const [permissionCatalog, setPermissionCatalog] = useState<PermissionCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatingMember, setUpdatingMember] = useState('')
  const [departmentName, setDepartmentName] = useState('')
  const [parentOrganizationId, setParentOrganizationId] = useState('')
  const [creatingOrganization, setCreatingOrganization] = useState(false)
  const [editingOrganizationId, setEditingOrganizationId] = useState('')
  const [editedOrganizationName, setEditedOrganizationName] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<{ tenant_id: string; members: TenantMember[]; invitations: MemberInvitation[]; seat_limit: number; seats_used: number }>(
        '/api/v1/members',
        {},
        tenant.id,
      ),
      apiRequest<{ tenant_id: string; organizations: Organization[] }>(
        '/api/v1/organizations',
        {},
        tenant.id,
      ),
      apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenant.id),
      apiRequest<PermissionCatalog>('/api/v1/roles/catalog', {}, tenant.id),
    ])
      .then(([memberResult, organizationResult, roleResult, catalog]) => {
        if (!active) return
        setMembers(memberResult.members)
        setInvitations(memberResult.invitations)
        setSeatLimit(memberResult.seat_limit)
        setSeatsUsed(memberResult.seats_used)
        setOrganizations(organizationResult.organizations)
        setTenantRoles(roleResult.roles)
        setPermissionCatalog(catalog)
        setParentOrganizationId(
          organizationResult.organizations.find((organization) => organization.is_root)?.id ?? '',
        )
        setInviteOrganizationId(
          organizationResult.organizations.find((organization) => organization.is_root)?.id ?? '',
        )
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [tenant.id])

  async function refreshMembers() {
    const result = await apiRequest<{
      members: TenantMember[]
      invitations: MemberInvitation[]
      seat_limit: number
      seats_used: number
    }>('/api/v1/members', {}, tenant.id)
    setMembers(result.members)
    setInvitations(result.invitations)
    setSeatLimit(result.seat_limit)
    setSeatsUsed(result.seats_used)
  }

  async function createInvitations(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setInviting(true)
    setError('')
    try {
      const emails = inviteEmails.split(/[,\n;]/).map((email) => email.trim()).filter(Boolean)
      const isCustom = inviteRole.startsWith('custom:')
      const result = await apiRequest<{
        invitations: CreatedMemberInvitation[]
        seat_limit: number
        seats_used: number
      }>('/api/v1/members/invitations', {
        method: 'POST',
        body: JSON.stringify({
          emails,
          role: isCustom ? 'custom' : inviteRole,
          custom_role_id: isCustom ? inviteRole.slice('custom:'.length) : null,
          organization_id: inviteOrganizationId,
        }),
      }, tenant.id)
      setInvitations((current) => [...result.invitations, ...current])
      setOneTimeLinks((current) => ({
        ...current,
        ...Object.fromEntries(result.invitations.map((item) => [item.id, item.invitation_url])),
      }))
      setSeatLimit(result.seat_limit)
      setSeatsUsed(result.seats_used)
      setInviteEmails('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建邀请失败')
    } finally {
      setInviting(false)
    }
  }

  async function resendInvitation(invitation: MemberInvitation) {
    setError('')
    try {
      const renewed = await apiRequest<CreatedMemberInvitation>(
        `/api/v1/members/invitations/${invitation.id}/resend`,
        { method: 'POST' },
        tenant.id,
      )
      setInvitations((current) => current.map((item) => item.id === renewed.id ? renewed : item))
      setOneTimeLinks((current) => ({ ...current, [renewed.id]: renewed.invitation_url }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '重新生成邀请链接失败')
    }
  }

  async function revokeInvitation(invitation: MemberInvitation) {
    if (!window.confirm(`撤销发送给 ${invitation.email} 的邀请？旧邀请链接将立即失效。`)) return
    setError('')
    try {
      await apiRequest<void>(
        `/api/v1/members/invitations/${invitation.id}`,
        { method: 'DELETE' },
        tenant.id,
      )
      setOneTimeLinks((current) => {
        const next = { ...current }
        delete next[invitation.id]
        return next
      })
      await refreshMembers()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '撤销邀请失败')
    }
  }

  async function copyInvitationLink(invitationId: string) {
    const link = oneTimeLinks[invitationId]
    if (!link) return
    try {
      await navigator.clipboard.writeText(link)
    } catch {
      setError('无法访问剪贴板，请检查浏览器权限后重试')
    }
  }

  async function changeMemberStatus(member: TenantMember) {
    const status = member.status === 'active' ? 'suspended' : 'active'
    const action = status === 'suspended' ? '暂停' : '恢复'
    if (!window.confirm(`${action}${member.display_name}？${status === 'suspended' ? '其当前登录会话将立即失效。' : '恢复操作需要有可用席位。'}`)) return
    setUpdatingMember(member.id)
    setError('')
    try {
      await apiRequest<TenantMember>(`/api/v1/members/${member.id}/status`, {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      }, tenant.id)
      if (member.id === currentUserId && status === 'suspended') {
        window.location.reload()
        return
      }
      await refreshMembers()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : `${action}成员失败`)
    } finally {
      setUpdatingMember('')
    }
  }

  async function removeMember(member: TenantMember) {
    if (!window.confirm(`移除 ${member.display_name}？该操作不能撤销；其企业成员关系将删除。`)) return
    setUpdatingMember(member.id)
    setError('')
    try {
      await apiRequest<void>(`/api/v1/members/${member.id}`, { method: 'DELETE' }, tenant.id)
      if (member.id === currentUserId) {
        window.location.reload()
        return
      }
      await refreshMembers()
      await onMembershipChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '移除成员失败')
    } finally {
      setUpdatingMember('')
    }
  }

  async function updateRole(member: TenantMember, selectedRole: string) {
    setUpdatingMember(member.id)
    setError('')
    try {
      const isCustom = selectedRole.startsWith('custom:')
      const role = isCustom ? 'custom' : selectedRole
      const updated = await apiRequest<TenantMember>(
        `/api/v1/members/${member.id}/role`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            role,
            custom_role_id: isCustom ? selectedRole.slice('custom:'.length) : null,
          }),
        },
        tenant.id,
      )
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item))
      const refreshedRoles = await apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenant.id)
      setTenantRoles(refreshedRoles.roles)
      await onMembershipChanged()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '角色更新失败')
    } finally {
      setUpdatingMember('')
    }
  }

  async function createOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setCreatingOrganization(true)
    setError('')
    try {
      await apiRequest<Organization>(
        '/api/v1/organizations',
        {
          method: 'POST',
          body: JSON.stringify({
            name: departmentName,
            parent_id: parentOrganizationId || null,
          }),
        },
        tenant.id,
      )
      const refreshed = await apiRequest<{ tenant_id: string; organizations: Organization[] }>(
        '/api/v1/organizations',
        {},
        tenant.id,
      )
      setOrganizations(refreshed.organizations)
      setDepartmentName('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门创建失败')
    } finally {
      setCreatingOrganization(false)
    }
  }

  async function saveOrganizationName(organization: Organization) {
    setError('')
    try {
      const updated = await apiRequest<Organization>(
        `/api/v1/organizations/${organization.id}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ name: editedOrganizationName }),
        },
        tenant.id,
      )
      setOrganizations((current) => current.map((item) => item.id === updated.id ? updated : item))
      setEditingOrganizationId('')
      setEditedOrganizationName('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门名称更新失败')
    }
  }

  async function deleteOrganization(organization: Organization) {
    if (!window.confirm(`删除部门“${organization.name}”？此操作不可撤销。`)) return
    setError('')
    try {
      await apiRequest<void>(
        `/api/v1/organizations/${organization.id}`,
        { method: 'DELETE' },
        tenant.id,
      )
      setOrganizations((current) => current.filter((item) => item.id !== organization.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '部门删除失败')
    }
  }

  async function changeOrganization(member: TenantMember, organizationId: string) {
    setUpdatingMember(member.id)
    setError('')
    try {
      const updated = await apiRequest<TenantMember>(
        `/api/v1/members/${member.id}/organization`,
        {
          method: 'PATCH',
          body: JSON.stringify({ organization_id: organizationId }),
        },
        tenant.id,
      )
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item))
      setOrganizations((current) => current.map((item) => {
        const delta = item.id === updated.organization_id ? 1 : item.id === member.organization_id ? -1 : 0
        return { ...item, member_count: item.member_count + delta }
      }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '成员部门更新失败')
    } finally {
      setUpdatingMember('')
    }
  }

  return (
    <section className="members-page">
      <div className="members-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />企业访问控制</div>
          <h1>成员与权限</h1>
          <p>管理谁可以进入 {tenant.name}，以及他们在工作区内可以执行的操作。</p>
        </div>
        <div className="members-count"><strong>{members.length}</strong><span>位成员</span></div>
      </div>
      <div className="members-note"><ShieldCheck size={17} /><span>角色只在当前企业工作区生效。平台管理员权限与企业角色分开管理。</span></div>
      <div className="panel members-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">成员席位</div><h2>已用 {seatsUsed} / {seatLimit} 个席位</h2></div>
          <span className="tenant-role-chip">启用成员与有效邀请计入席位</span>
        </div>
        <p className="organization-description">邀请链接有效 7 天。当前未配置邮件投递，请复制链接并通过安全渠道转发；链接仅在创建或重新生成时显示。</p>
        <form className="organization-create-form invitation-form" onSubmit={(event) => void createInvitations(event)}>
          <label>
            <span>邀请邮箱</span>
            <textarea
              aria-label="邀请邮箱"
              onChange={(event) => setInviteEmails(event.target.value)}
              placeholder="name@company.com，可输入多个邮箱，以逗号或换行分隔"
              required
              rows={3}
              value={inviteEmails}
            />
          </label>
          <label>
            <span>角色</span>
            <select onChange={(event) => setInviteRole(event.target.value)} value={inviteRole}>
              {tenantRoles.filter((role) => role.is_builtin || role.is_active).map((role) => (
                <option key={role.key} value={role.key}>{role.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span>所属部门</span>
            <select onChange={(event) => setInviteOrganizationId(event.target.value)} required value={inviteOrganizationId}>
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>{'　'.repeat(organization.depth)}{organization.name}</option>
              ))}
            </select>
          </label>
          <button className="primary-button" disabled={inviting || !inviteEmails.trim() || !inviteOrganizationId} type="submit">
            <Plus size={15} />{inviting ? '正在创建邀请…' : '创建邀请链接'}
          </button>
        </form>
        {invitations.length > 0 && (
          <div className="invitation-list">
            {invitations.map((invitation) => {
              const invitationStatus = invitation.status
              const statusLabels: Record<string, string> = {
                pending: '已邀请',
                accepted: '已接受',
                expired: '已过期',
                revoked: '已撤销',
              }
              return (
                <div className="invitation-row" key={invitation.id}>
                  <span className="invitation-copy"><strong>{invitation.email}</strong><small>{roleLabel(invitation.role, invitation.custom_role_name)} · {invitation.organization_name}</small></span>
                  <span className={`member-status ${invitationStatus === 'pending' ? 'active' : 'inactive'}`}><i />{statusLabels[invitationStatus]}</span>
                  {oneTimeLinks[invitation.id] && invitationStatus === 'pending' && (
                    <button
                      className="text-action"
                      onClick={() => void copyInvitationLink(invitation.id)}
                      type="button"
                    >复制邀请链接</button>
                  )}
                  {(invitationStatus === 'pending' || invitationStatus === 'expired') && (
                    <button className="text-action" onClick={() => void resendInvitation(invitation)} type="button">重新生成</button>
                  )}
                  {invitationStatus === 'pending' && (
                    <button className="text-action danger-action" onClick={() => void revokeInvitation(invitation)} type="button">撤销</button>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
      {permissionCatalog && (
        <RoleManagementPanel
          catalog={permissionCatalog}
          organizations={organizations}
          roles={tenantRoles}
          tenantId={tenant.id}
          onRolesChanged={setTenantRoles}
        />
      )}
      <div className="panel members-panel organization-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">组织架构</div><h2>部门与成员范围</h2></div>
          <span className="tenant-role-chip">{organizations.length} 个部门</span>
        </div>
        <p className="organization-description">部门用于成员分组与数据范围授权。根组织受保护；有成员或子部门的部门不能删除。</p>
        {error && <div className="members-error" role="alert">{error}</div>}
        {!loading && organizations.length > 0 && (
          <div className="organization-list" role="list" aria-label="部门列表">
            {organizations.map((organization) => (
              <div className="organization-row" key={organization.id} role="listitem">
                <span className={`organization-marker${organization.is_root ? ' root' : ''}`} style={{ marginLeft: organization.depth * 20 }}>
                  <Building2 size={15} />
                </span>
                {editingOrganizationId === organization.id ? (
                  <form
                    className="organization-rename"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void saveOrganizationName(organization)
                    }}
                  >
                    <input
                      aria-label="部门名称"
                      autoFocus
                      maxLength={80}
                      onChange={(event) => setEditedOrganizationName(event.target.value)}
                      required
                      value={editedOrganizationName}
                    />
                    <button className="text-action" type="submit">保存</button>
                    <button className="text-action" onClick={() => setEditingOrganizationId('')} type="button">取消</button>
                  </form>
                ) : (
                  <>
                    <span className="organization-copy">
                      <strong>{organization.name}</strong>
                      <small>{organization.is_root ? '根组织' : `${organization.member_count} 位成员 · ${organization.child_count} 个子部门`}</small>
                    </span>
                    {!organization.is_root && (
                      <span className="organization-actions">
                        <button
                          aria-label={`重命名${organization.name}`}
                          className="icon-button"
                          onClick={() => {
                            setEditingOrganizationId(organization.id)
                            setEditedOrganizationName(organization.name)
                          }}
                          type="button"
                        ><Pencil size={14} /></button>
                        <button
                          aria-label={`删除${organization.name}`}
                          className="icon-button organization-delete"
                          onClick={() => void deleteOrganization(organization)}
                          type="button"
                        ><Trash2 size={14} /></button>
                      </span>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        )}
        {!loading && organizations.length === 0 && !error && (
          <div className="members-empty">尚未设置组织架构。请刷新页面重试，或联系平台管理员检查租户初始化。</div>
        )}
        <form className="organization-create-form" onSubmit={createOrganization}>
          <label>
            <span>新部门名称</span>
            <input
              maxLength={80}
              onChange={(event) => setDepartmentName(event.target.value)}
              placeholder="例如：客户成功部"
              required
              value={departmentName}
            />
          </label>
          <label>
            <span>上级部门</span>
            <select
              onChange={(event) => setParentOrganizationId(event.target.value)}
              required
              value={parentOrganizationId}
            >
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {'　'.repeat(organization.depth)}{organization.name}
                </option>
              ))}
            </select>
          </label>
          <button className="primary-button" disabled={creatingOrganization || !departmentName.trim()} type="submit">
            <Plus size={15} />{creatingOrganization ? '正在创建…' : '新增部门'}
          </button>
        </form>
      </div>
      <div className="panel members-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">工作区成员</div><h2>{tenant.name}</h2></div>
          <span className="tenant-role-chip">{roleLabel(tenant.role, tenant.custom_role_name)}</span>
        </div>
        {loading ? (
          <div className="members-empty">正在读取成员列表…</div>
        ) : members.length === 0 ? (
          <div className="members-empty">此工作区还没有成员。</div>
        ) : (
          <div className="members-table-wrap">
            <table className="members-table">
              <thead><tr><th>成员</th><th>所属部门</th><th>角色</th><th>成员状态</th><th>操作</th></tr></thead>
              <tbody>
                {members.map((member) => (
                  <tr key={member.id}>
                    <td>
                      <div className="member-cell">
                        <span className="member-avatar">{member.display_name.slice(0, 1)}</span>
                        <span><strong>{member.display_name}</strong><small>{member.email}</small></span>
                      </div>
                    </td>
                    <td>
                      <select
                        aria-label={`${member.display_name}的所属部门`}
                        className="role-select"
                        disabled={updatingMember === member.id || member.status !== 'active'}
                        onChange={(event) => void changeOrganization(member, event.target.value)}
                        value={member.organization_id}
                      >
                        {organizations.map((organization) => (
                          <option key={organization.id} value={organization.id}>
                            {'　'.repeat(organization.depth)}{organization.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {tenant.role === 'tenant_admin' ? (
                        <select
                          aria-label={`${member.display_name}的角色`}
                          className="role-select"
                          disabled={updatingMember === member.id || member.id === currentUserId || member.status !== 'active'}
                          onChange={(event) => void updateRole(member, event.target.value)}
                          value={member.role === 'custom' ? `custom:${member.custom_role_id}` : member.role}
                        >
                          {tenantRoles.filter((role) => role.is_builtin || role.is_active).map((role) => (
                            <option key={role.key} value={role.key}>
                              {role.name}
                            </option>
                          ))}
                        </select>
                      ) : <span className="role-text">{roleLabel(member.role, member.custom_role_name)}</span>}
                    </td>
                    <td><span className={`member-status ${member.status === 'active' ? 'active' : 'inactive'}`}><i />{member.status === 'active' ? '启用' : '已暂停'}</span></td>
                    <td className="member-actions-cell">
                      <button
                        className="text-action"
                        disabled={updatingMember === member.id}
                        onClick={() => void changeMemberStatus(member)}
                        type="button"
                      >{member.status === 'active' ? '暂停' : '恢复'}</button>
                      <button
                        className="text-action danger-action"
                        disabled={updatingMember === member.id}
                        onClick={() => void removeMember(member)}
                        type="button"
                      >移除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
