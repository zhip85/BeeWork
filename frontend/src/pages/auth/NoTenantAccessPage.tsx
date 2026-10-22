import {
  Building2,
  LogOut,
} from 'lucide-react'
import type { CurrentUser } from '../../types'

export function NoTenantAccessPage({
  user,
  onLogout,
}: {
  user: CurrentUser
  onLogout: () => Promise<void>
}) {
  return (
    <main className="no-tenant-screen">
      <section className="no-tenant-card">
        <span className="platform-identity-icon"><Building2 size={19} /></span>
        <div className="section-kicker">企业工作区不可用</div>
        <h1>当前账号没有可用的企业</h1>
        <p>{user.email} 尚未加入运行中的企业工作区。请联系企业管理员；如果企业刚刚恢复服务，请重新登录。</p>
        <button className="primary-button" onClick={() => void onLogout()} type="button"><LogOut size={15} />退出登录</button>
      </section>
    </main>
  )
}
