import { useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { apiRequest } from '../../api'
import type { CurrentUser } from '../../types'

export function InviteAcceptancePage({
  onAccepted,
}: {
  onAccepted: (user: CurrentUser) => void
}) {
  const location = useLocation()
  const token = new URLSearchParams(location.search).get('token') ?? ''
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  async function acceptInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const user = await apiRequest<CurrentUser>('/api/v1/members/invitations/accept', {
        method: 'POST',
        body: JSON.stringify({
          token,
          password,
          display_name: displayName.trim() || null,
        }),
      })
      onAccepted(user)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '接受邀请失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-screen">
      <section className="auth-card">
        <div className="eyebrow"><span className="eyebrow-dot" />企业工作区邀请</div>
        <h1>接受成员邀请</h1>
        <p>设置登录密码后即可进入企业工作区。已有 BeeWork 账号的成员请使用原账号密码。</p>
        {!token && <div className="members-error" role="alert">邀请链接缺少有效凭据，请向企业管理员索取新链接。</div>}
        {error && <div className="members-error" role="alert">{error}</div>}
        <form className="login-form" onSubmit={(event) => void acceptInvitation(event)}>
          <label>姓名<input autoComplete="name" maxLength={120} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
          <label>密码<input autoComplete="new-password" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} /><small>至少 12 位；已有账号请输入当前密码。</small></label>
          <button className="primary-button" disabled={!token || submitting} type="submit">{submitting ? '正在接受邀请…' : '接受邀请并进入工作区'}</button>
        </form>
      </section>
    </main>
  )
}
