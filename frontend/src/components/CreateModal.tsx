import {
  Sparkles,
  X,
} from 'lucide-react'

export function CreateModal({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="create-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={(event) => event.stopPropagation()}>
        <button className="modal-close" type="button" aria-label="关闭" onClick={onClose}><X size={18} /></button>
        <div className="modal-symbol"><Sparkles size={19} /></div>
        <div className="section-kicker">新建工作</div>
        <h2 id="modal-title">{title}</h2>
        <p>页面框架已准备好，配置表单和保存能力会在模块开发时接入。</p>
        <button className="primary-button" type="button" onClick={onClose}>知道了</button>
      </div>
    </div>
  )
}

