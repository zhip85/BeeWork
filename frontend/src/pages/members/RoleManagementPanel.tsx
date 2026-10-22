import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  Plus,
  X,
} from 'lucide-react'
import { apiRequest } from '../../api'
import type { Organization, PermissionCatalog, RoleDraft, TenantRole } from '../../types'

export function RoleManagementPanel({
  catalog,
  organizations,
  roles,
  tenantId,
  onRolesChanged,
}: {
  catalog: PermissionCatalog
  organizations: Organization[]
  roles: TenantRole[]
  tenantId: string
  onRolesChanged: (roles: TenantRole[]) => void
}) {
  const [draft, setDraft] = useState<RoleDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  function startNewRole(source?: TenantRole) {
    setError('')
    setDraft({
      id: null,
      name: source ? `${source.name}自定义` : '',
      description: source ? `基于「${source.name}」创建` : '',
      scope: 'tenant',
      scope_organization_id: '',
      permissions: source
        ? Object.fromEntries(
            Object.entries(source.permissions).map(([module, actions]) => [module, [...actions]]),
          )
        : {},
      is_active: true,
    })
  }

  async function refreshRoles() {
    const result = await apiRequest<{ roles: TenantRole[] }>('/api/v1/roles', {}, tenantId)
    onRolesChanged(result.roles)
  }

  async function saveRole(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft) return
    setSaving(true)
    setError('')
    try {
      const payload = {
        name: draft.name,
        description: draft.description,
        scope: draft.scope,
        scope_organization_id: draft.scope === 'organization' ? draft.scope_organization_id : null,
        permissions: draft.permissions,
        is_active: draft.is_active,
      }
      await apiRequest<TenantRole>(
        draft.id ? `/api/v1/roles/${draft.id}` : '/api/v1/roles',
        {
          method: draft.id ? 'PUT' : 'POST',
          body: JSON.stringify(payload),
        },
        tenantId,
      )
      await refreshRoles()
      setDraft(null)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存角色失败')
    } finally {
      setSaving(false)
    }
  }

  async function deleteRole(role: TenantRole) {
    if (!role.id || !window.confirm(`删除角色“${role.name}”？此操作不可撤销。`)) return
    setError('')
    try {
      await apiRequest<void>(`/api/v1/roles/${role.id}`, { method: 'DELETE' }, tenantId)
      await refreshRoles()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '删除角色失败')
    }
  }

  function togglePermission(module: string, action: string, enabled: boolean) {
    setDraft((current) => {
      if (!current) return current
      const actions = new Set(current.permissions[module] ?? [])
      if (enabled) actions.add(action)
      else actions.delete(action)
      const permissions = { ...current.permissions }
      if (actions.size) permissions[module] = [...actions]
      else delete permissions[module]
      return { ...current, permissions }
    })
  }

  return (
    <div className="panel role-management-panel">
      <div className="members-table-heading role-panel-heading">
        <div><div className="section-kicker">访问策略</div><h2>角色与权限</h2></div>
        <button className="primary-button" onClick={() => startNewRole()} type="button">
          <Plus size={15} />新建自定义角色
        </button>
      </div>
      <p className="organization-description">内置角色不可修改；自定义角色可按模块配置操作权限，并限定在租户或指定部门范围内。</p>
      {error && <div className="members-error" role="alert">{error}</div>}
      <div className="role-card-grid">
        {roles.map((role) => (
          <article className={`role-card${role.is_active ? '' : ' is-inactive'}`} key={role.key}>
            <div className="role-card-heading">
              <div><strong>{role.name}</strong><small>{role.is_builtin ? '系统内置' : role.scope === 'tenant' ? '全企业范围' : '部门范围'}</small></div>
              {!role.is_active && <span className="role-inactive-label">已停用</span>}
            </div>
            <p>{role.description || '未填写角色说明'}</p>
            <div className="role-card-footer">
              <span>{role.member_count} 位成员</span>
              {role.is_builtin ? (
                <button className="text-action" onClick={() => startNewRole(role)} type="button">以此角色创建</button>
              ) : (
                <span className="role-card-actions">
                  <button
                    className="text-action"
                    onClick={() => {
                      setError('')
                      setDraft({
                        id: role.id,
                        name: role.name,
                        description: role.description,
                        scope: role.scope,
                        scope_organization_id: role.scope_organization_id ?? '',
                        permissions: Object.fromEntries(
                          Object.entries(role.permissions).map(([module, actions]) => [module, [...actions]]),
                        ),
                        is_active: role.is_active,
                      })
                    }}
                    type="button"
                  >编辑</button>
                  <button
                    className="text-action danger-text-action"
                    disabled={role.member_count > 0}
                    onClick={() => void deleteRole(role)}
                    type="button"
                  >删除</button>
                </span>
              )}
            </div>
          </article>
        ))}
      </div>
      {draft && (
        <form className="role-editor" onSubmit={(event) => void saveRole(event)}>
          <div className="role-editor-heading">
            <div><div className="section-kicker">{draft.id ? '编辑自定义角色' : '新建自定义角色'}</div><h3>定义访问范围与操作权限</h3></div>
            <button aria-label="关闭角色编辑器" className="icon-button" onClick={() => setDraft(null)} type="button"><X size={17} /></button>
          </div>
          <div className="role-form-fields">
            <label><span>角色名称</span><input maxLength={80} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} /></label>
            <label><span>说明</span><input maxLength={500} onChange={(event) => setDraft({ ...draft, description: event.target.value })} value={draft.description} /></label>
            <label>
              <span>数据范围</span>
              <select
                onChange={(event) => setDraft({
                  ...draft,
                  scope: event.target.value as RoleDraft['scope'],
                  scope_organization_id: event.target.value === 'tenant' ? '' : (organizations[0]?.id ?? ''),
                })}
                value={draft.scope}
              >
                <option value="tenant">全企业</option>
                <option value="organization">指定部门及下级</option>
              </select>
            </label>
            {draft.scope === 'organization' && (
              <label>
                <span>授权部门</span>
                <select onChange={(event) => setDraft({ ...draft, scope_organization_id: event.target.value })} required value={draft.scope_organization_id}>
                  {organizations.map((organization) => (
                    <option key={organization.id} value={organization.id}>
                      {'　'.repeat(organization.depth)}{organization.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="permission-matrix-wrap">
            <table className="permission-matrix">
              <thead><tr><th>功能模块</th>{['view', 'edit', 'publish', 'delete', 'test', 'intervene', 'export', 'approve'].map((action) => (
                <th key={action}>{catalog.action_labels[action] ?? action}</th>
              ))}</tr></thead>
              <tbody>{catalog.modules.map((module) => (
                <tr key={module.key}>
                  <th>{module.label}</th>
                  {['view', 'edit', 'publish', 'delete', 'test', 'intervene', 'export', 'approve'].map((action) => (
                    <td key={action}>
                      {module.actions.includes(action) && (
                        <input
                          aria-label={`${module.label}${catalog.action_labels[action] ?? action}`}
                          checked={(draft.permissions[module.key] ?? []).includes(action)}
                          onChange={(event) => togglePermission(module.key, action, event.target.checked)}
                          type="checkbox"
                        />
                      )}
                    </td>
                  ))}
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="role-editor-footer">
            {draft.id && <label className="role-active-toggle"><input checked={draft.is_active} onChange={(event) => setDraft({ ...draft, is_active: event.target.checked })} type="checkbox" />角色启用</label>}
            <div>
              <button className="text-action" onClick={() => setDraft(null)} type="button">取消</button>
              <button className="primary-button" disabled={saving || !draft.name.trim()} type="submit">{saving ? '正在保存…' : '保存角色'}</button>
            </div>
          </div>
        </form>
      )}
    </div>
  )
}
