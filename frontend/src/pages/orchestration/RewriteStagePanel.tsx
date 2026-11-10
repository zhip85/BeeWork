import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiRequest } from '../../api'
import type { AssistantModelOptions } from '../assistants/types'
import type { StagePanelProps } from './stagePanels'

type RewriteConfig = {
  enabled: boolean
  model_id: string | null
  only_low_confidence: boolean
  confidence_threshold: number
  coreference: { enabled: boolean; rounds: number }
  normalize: boolean
  decompose: { enabled: boolean; max_subquestions: number }
  translate: { enabled: boolean; language: string }
  custom_prompt: { enabled: boolean; text: string }
}
type TestResult = { steps: { step: string; before: string; after: string }[]; query: string; sub_questions: string[]; synonyms: string[]; failure: string | null; custom_prompt: boolean }

const DEFAULTS: RewriteConfig = {
  enabled: false, model_id: null, only_low_confidence: true, confidence_threshold: 0.7, coreference: { enabled: true, rounds: 3 }, normalize: true,
  decompose: { enabled: false, max_subquestions: 3 }, translate: { enabled: false, language: 'English' }, custom_prompt: { enabled: false, text: '' },
}
const LANGUAGES = ['English', '中文', '日本語']

/** OC · question rewrite (OC-040 – OC-051). The configuration is the draft OC; the test box runs the unsaved settings on a sample question. */
export function RewriteStagePanel({ assistantId, tenant, oc, readOnly, onChange }: StagePanelProps) {
  const { t } = useTranslation('orchestration')
  const config: RewriteConfig = { ...DEFAULTS, ...((oc.rewrite ?? {}) as Partial<RewriteConfig>) }
  const [models, setModels] = useState<AssistantModelOptions['models']>([])
  const [question, setQuestion] = useState('')
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<TestResult | null>(null)
  const [error, setError] = useState('')
  const set = (next: Partial<RewriteConfig>) => onChange({ ...oc, rewrite: { ...config, ...next } })
  const off = readOnly || !config.enabled

  useEffect(() => {
    let active = true
    apiRequest<AssistantModelOptions>('/api/v1/assistants/model-options', {}, tenant.id).then((options) => active && setModels(options.models)).catch(() => undefined)
    return () => { active = false }
  }, [tenant.id])

  async function runTest() {
    setTesting(true)
    setError('')
    try {
      setResult(await apiRequest<TestResult>(`/api/v1/assistants/${assistantId}/oc/rewrite-test`, { method: 'POST', body: JSON.stringify({ question, rewrite: config, glossary: oc.glossary ?? null }) }, tenant.id))
    } catch (requestError) {
      setResult(null)
      setError(requestError instanceof Error ? requestError.message : t('actionFailed'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="oc-rewrite-panel">
      <p className="usage-hint">{t('rewrite.hint')}</p>
      <label className="governance-inherit">
        <input checked={config.enabled} disabled={readOnly} onChange={(event) => set({ enabled: event.target.checked })} type="checkbox" />
        <span><strong>{t('rewrite.enable')}</strong><small>{t('rewrite.enableHint')}</small></span>
      </label>
      <label className="rewrite-field">{t('rewrite.model')}
        <select disabled={off} onChange={(event) => set({ model_id: event.target.value || null })} value={config.model_id ?? ''}>
          <option value="">{t('rewrite.modelDefault')}</option>
          {models.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
        </select>
        <small>{t('rewrite.modelHint')}</small>
      </label>
      <label className="governance-inherit">
        <input checked={config.only_low_confidence} disabled={off} onChange={(event) => set({ only_low_confidence: event.target.checked })} type="checkbox" />
        <span><strong>{t('rewrite.lowConfidence')}</strong><small>{t('rewrite.lowConfidenceHint')}</small></span>
      </label>
      {config.only_low_confidence && (
        <label className="rewrite-field">{t('rewrite.threshold', { value: config.confidence_threshold.toFixed(2) })}
          <input disabled={off} max={0.95} min={0.3} onChange={(event) => set({ confidence_threshold: Number(event.target.value) })} step={0.05} type="range" value={config.confidence_threshold} />
        </label>
      )}
      <fieldset className="rewrite-steps" disabled={off}>
        <legend>{t('rewrite.strategies')}</legend>
        <label className="governance-inherit">
          <input checked={config.coreference.enabled} onChange={(event) => set({ coreference: { ...config.coreference, enabled: event.target.checked } })} type="checkbox" />
          <span><strong>{t('rewrite.coreference')}</strong><small>{t('rewrite.coreferenceHint')}</small></span>
        </label>
        {config.coreference.enabled && (
          <label className="rewrite-field">{t('rewrite.rounds')}
            <input max={10} min={1} onChange={(event) => set({ coreference: { ...config.coreference, rounds: Math.min(10, Math.max(1, Number(event.target.value) || 1)) } })} type="number" value={config.coreference.rounds} />
          </label>
        )}
        <label className="governance-inherit">
          <input checked={config.normalize} onChange={(event) => set({ normalize: event.target.checked })} type="checkbox" />
          <span><strong>{t('rewrite.normalize')}</strong><small>{t('rewrite.normalizeHint')}</small></span>
        </label>
        <label className="governance-inherit">
          <input checked={config.decompose.enabled} onChange={(event) => set({ decompose: { ...config.decompose, enabled: event.target.checked } })} type="checkbox" />
          <span><strong>{t('rewrite.decompose')}</strong><small>{t('rewrite.decomposeHint')}</small></span>
        </label>
        {config.decompose.enabled && (
          <label className="rewrite-field">{t('rewrite.maxSub')}
            <input max={6} min={2} onChange={(event) => set({ decompose: { ...config.decompose, max_subquestions: Math.min(6, Math.max(2, Number(event.target.value) || 2)) } })} type="number" value={config.decompose.max_subquestions} />
          </label>
        )}
        <label className="governance-inherit">
          <input checked={config.translate.enabled} onChange={(event) => set({ translate: { ...config.translate, enabled: event.target.checked } })} type="checkbox" />
          <span><strong>{t('rewrite.translate')}</strong><small>{t('rewrite.translateHint')}</small></span>
        </label>
        {config.translate.enabled && (
          <label className="rewrite-field">{t('rewrite.language')}
            <select onChange={(event) => set({ translate: { ...config.translate, language: event.target.value } })} value={config.translate.language}>
              {LANGUAGES.map((language) => <option key={language} value={language}>{language}</option>)}
            </select>
          </label>
        )}
        <label className="governance-inherit">
          <input checked={config.custom_prompt.enabled} onChange={(event) => set({ custom_prompt: { ...config.custom_prompt, enabled: event.target.checked } })} type="checkbox" />
          <span><strong>{t('rewrite.custom')}</strong><small>{t('rewrite.customHint')}</small></span>
        </label>
        {config.custom_prompt.enabled && (
          <label className="rewrite-field">
            <textarea aria-label={t('rewrite.custom')} maxLength={2000} onChange={(event) => set({ custom_prompt: { ...config.custom_prompt, text: event.target.value } })} rows={4} value={config.custom_prompt.text} />
            <small>{config.custom_prompt.text.length} / 2000</small>
          </label>
        )}
      </fieldset>

      <div className="panel rewrite-test">
        <h3>{t('rewrite.testTitle')}</h3>
        <p className="usage-hint">{t('rewrite.testHint')}</p>
        <form className="rewrite-test-form" onSubmit={(event) => { event.preventDefault(); void runTest() }}>
          <input aria-label={t('rewrite.testTitle')} maxLength={500} onChange={(event) => setQuestion(event.target.value)} placeholder={t('rewrite.testPlaceholder')} value={question} />
          <button className="secondary-button" disabled={readOnly || testing || !question.trim()} type="submit">{testing ? t('rewrite.testing') : t('rewrite.run')}</button>
        </form>
        {error && <p className="form-error" role="alert">{error}</p>}
        {result && (
          <div className="rewrite-result" role="status">
            {result.failure && <p className="assistants-notice">{t(`rewrite.failure.${result.failure}`)}</p>}
            {result.steps.length === 0 ? <p className="assistants-empty-filtered">{t('rewrite.noChange')}</p> : (
              <ol>
                {result.steps.map((step) => <li key={step.step}><strong>{t(`rewrite.step.${step.step}`)}</strong><span>{step.before}</span><b aria-hidden="true">→</b><span>{step.after}</span></li>)}
              </ol>
            )}
            <p><strong>{t('rewrite.finalQuery')}</strong> {result.query}</p>
          </div>
        )}
      </div>
    </div>
  )
}
