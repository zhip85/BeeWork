import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  ArrowRight,
  Hexagon,
} from 'lucide-react'
import { Link } from 'react-router-dom'

export function LoginPage({
  onLogin,
  error,
}: {
  onLogin: (email: string, password: string) => Promise<void>
  error: string
}) {
  const [loginType, setLoginType] = useState<'tenant' | 'platform'>('tenant')
  const [email, setEmail] = useState(import.meta.env.DEV ? 'admin@beework.dev' : '')
  const [password, setPassword] = useState(import.meta.env.DEV ? 'Beework-Dev-2026!' : '')
  const [formError, setFormError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setFormError('')
    try {
      await onLogin(email, password)
    } catch (loginError) {
      setFormError(loginError instanceof Error ? loginError.message : '登录失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-screen">
      <section className="login-panel">
        <Link className="brand login-brand" to="/" aria-label="BeeWork">
          <span className="brand-mark"><Hexagon size={22} strokeWidth={2.2} /><span /></span>
          <span className="brand-word">BeeWork</span>
          <span className="brand-edition">TAP</span>
        </Link>
        <div className="login-eyebrow"><span className="eyebrow-dot" />{loginType === 'platform' ? 'BeeWork 平台运营后台' : '企业 AI 工作空间'}</div>
        <h1>{loginType === 'platform' ? <>管理平台，<br />服务企业。</> : <>让工作，<br />从容一点。</>}</h1>
        <p className="login-description">{loginType === 'platform' ? '平台管理员管理租户与平台运营，不属于任何企业工作区。' : '企业管理员和成员在此管理企业 AI 助理与工作流程。'}</p>
        <div className="login-type-switch" role="group" aria-label="选择登录入口">
          <button
            aria-pressed={loginType === 'tenant'}
            className={loginType === 'tenant' ? 'selected' : ''}
            onClick={() => {
              setLoginType('tenant')
              if (import.meta.env.DEV) {
                setEmail('admin@beework.dev')
                setPassword('Beework-Dev-2026!')
              }
            }}
            type="button"
          >企业工作区</button>
          <button
            aria-pressed={loginType === 'platform'}
            className={loginType === 'platform' ? 'selected' : ''}
            onClick={() => {
              setLoginType('platform')
              if (import.meta.env.DEV) {
                setEmail('platform@beework.dev')
                setPassword('BeeWork-Platform-2026!')
              }
            }}
            type="button"
          >SaaS 平台管理</button>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label htmlFor="login-email">{loginType === 'platform' ? '平台管理员邮箱' : '企业邮箱'}</label>
          <input
            autoComplete="username"
            id="login-email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
          <label htmlFor="login-password">密码</label>
          <input
            autoComplete="current-password"
            id="login-password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          {(formError || error) && <p className="login-error" role="alert">{formError || error}</p>}
          <button className="primary-button login-submit" disabled={submitting} type="submit">
            {submitting ? '正在登录…' : loginType === 'platform' ? '进入平台后台' : '登录企业工作区'} <ArrowRight size={16} />
          </button>
        </form>
        {import.meta.env.DEV && (
          <div className="login-demo-note">
            <strong>{loginType === 'platform' ? '本地 SaaS 平台管理员' : '本地企业管理员'}</strong>
            <span>{loginType === 'platform' ? 'platform@beework.dev' : 'admin@beework.dev'}</span>
            <span>{loginType === 'platform' ? 'BeeWork-Platform-2026!' : 'Beework-Dev-2026!'}</span>
          </div>
        )}
        <span className="login-footnote">BeeWork TAP · 企业 AI 平台</span>
      </section>
      <aside className="login-aside">
        <div className="login-aside-orbit orbit-one" />
        <div className="login-aside-orbit orbit-two" />
        <div className="login-aside-copy">
          <div className="login-aside-kicker">BUILD · RUN · GOVERN</div>
          <h2>让每一种<br />企业能力，<em>各就其位。</em></h2>
          <p>把知识、工具和 AI 团队，编织进值得信任的工作流程。</p>
          <div className="login-aside-bottom"><span>一处构建</span><i /><span>协同运行</span><i /><span>全程可治理</span></div>
        </div>
      </aside>
    </main>
  )
}
