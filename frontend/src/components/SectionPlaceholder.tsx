import { useState } from 'react'
import {
  Plus,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { CreateModal } from './CreateModal'

export function SectionPlaceholder({ title, icon, denied = false }: { title?: string; icon: React.ReactNode; denied?: boolean }) {
  const { t } = useTranslation()
  const [modalOpen, setModalOpen] = useState(false)
  const heading = denied ? t('accessDenied') : title ?? ''
  return (
    <section className="placeholder-page">
      <div className="placeholder-icon">{icon}</div>
      <div className="section-kicker">{t('placeholderKicker')}</div>
      <h1>{heading}</h1>
      <p>{denied ? t('accessDeniedBody') : t('placeholderBody')}</p>
      {!denied && (
        <button className="primary-button" type="button" onClick={() => setModalOpen(true)}><Plus size={17} />{t('createItem', { name: heading })}</button>
      )}
      {modalOpen && <CreateModal title={t('createItem', { name: heading })} onClose={() => setModalOpen(false)} />}
    </section>
  )
}
