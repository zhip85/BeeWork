import { useState } from 'react'
import { ArrowUp, LayoutGrid } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { PortalConfig } from './portalTypes'

const STYLES = ['concise', 'balanced', 'detailed'] as const

/** Home: greeting, the central input, and the app / style choices. Conversations arrive with T-143. */
export default function PortalHome({ config, name }: { config: PortalConfig; name: string }) {
  const { t } = useTranslation('portalApp')
  const [text, setText] = useState('')
  const [style, setStyle] = useState<PortalConfig['answer_style']>(config.answer_style)
  const hour = new Date().getHours()
  const part = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening'

  return (
    <section className="portal-home">
      <h1>{t(`greeting.${part}`, { name })}</h1>
      <p className="portal-home-sub">{t('askPrompt')}</p>
      <form className="portal-composer" onSubmit={(event) => event.preventDefault()}>
        <textarea
          aria-label={t('inputLabel')}
          onChange={(event) => setText(event.target.value)}
          placeholder={config.input_placeholder || t('askPrompt')}
          rows={3}
          value={text}
        />
        <div className="portal-composer-bar">
          <label className="portal-select">
            <span>{t('style')}</span>
            <select onChange={(event) => setStyle(event.target.value as PortalConfig['answer_style'])} value={style}>
              {STYLES.map((key) => <option key={key} value={key}>{t(`styles.${key}`)}</option>)}
            </select>
          </label>
          <label className="portal-select">
            <span>{t('appPicker')}</span>
            <select disabled value="">
              <option value="">{t('appPickerAll')}</option>
            </select>
          </label>
          {config.nav_app_library && <Link className="portal-link" to="/portal/apps"><LayoutGrid size={14} /> {t('browseApps')}</Link>}
          <button aria-label={t('send')} className="portal-send" disabled title={t('chatSoon')} type="submit"><ArrowUp size={16} /></button>
        </div>
      </form>
      <p className="portal-hint" role="status">{t('chatSoon')}</p>
    </section>
  )
}
