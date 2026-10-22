

export function roleLabel(role: string, customRoleName?: string | null) {
  if (role === 'custom' && customRoleName) return customRoleName
  const labels: Record<string, string> = {
    tenant_admin: '企业管理员',
    builder: '构建者',
    operator: '运营者',
    viewer: '查看者',
    auditor: '审计员',
    approver: '审计员',
    member: '查看者',
  }
  return labels[role] ?? role
}
