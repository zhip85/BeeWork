import { useState } from 'react'
import {
  Plus,
} from 'lucide-react'
import { CreateModal } from './CreateModal'

export function SectionPlaceholder({ title, icon }: { title: string; icon: React.ReactNode }) {
  const [modalOpen, setModalOpen] = useState(false)
  const isAccessDenied = title === '无权访问'
  return (
    <section className="placeholder-page">
      <div className="placeholder-icon">{icon}</div>
      <div className="section-kicker">工作空间</div>
      <h1>{title}</h1>
      <p>{isAccessDenied ? '当前账号没有访问此功能的权限，请联系对应的企业管理员或平台管理员。' : '前端框架已就绪，这个模块会在后续开发中接入真实 API。'}</p>
      {!isAccessDenied && (
        <button className="primary-button" type="button" onClick={() => setModalOpen(true)}><Plus size={17} />创建{title === '助理' ? '助理' : title === '知识库' ? '知识库' : '新项目'}</button>
      )}
      {modalOpen && <CreateModal title={title === '助理' ? '创建助理' : title === '知识库' ? '创建知识库' : `创建${title}`} onClose={() => setModalOpen(false)} />}
    </section>
  )
}
