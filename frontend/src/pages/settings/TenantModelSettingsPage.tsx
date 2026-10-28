import { useEffect, useState } from 'react'
import {
  Bot,
} from 'lucide-react'
import { apiRequest } from '../../api'
import type { ModelTestResult, TenantModelEnvironment } from '../../types'
import { enumLabel } from '../../i18n'

export function TenantModelSettingsPage({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const [environment, setEnvironment] = useState<TenantModelEnvironment['environment']>('production')
  const [config, setConfig] = useState<TenantModelEnvironment | null>(null)
  const [enabledModelIds, setEnabledModelIds] = useState<string[]>([])
  const [defaultModelIds, setDefaultModelIds] = useState<TenantModelEnvironment['default_model_ids']>({
    chat: null,
    embedding: null,
    rerank: null,
  })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    apiRequest<TenantModelEnvironment>(
      `/api/v1/tenant/model-environments/${environment}`,
      {},
      tenantId,
    )
      .then((result) => {
        if (!active) return
        const availableIds = new Set(result.models.map((model) => model.id))
        setConfig(result)
        setEnabledModelIds(result.enabled_model_ids)
        setDefaultModelIds({
          chat: result.default_model_ids.chat && availableIds.has(result.default_model_ids.chat) ? result.default_model_ids.chat : null,
          embedding: result.default_model_ids.embedding && availableIds.has(result.default_model_ids.embedding) ? result.default_model_ids.embedding : null,
          rerank: result.default_model_ids.rerank && availableIds.has(result.default_model_ids.rerank) ? result.default_model_ids.rerank : null,
        })
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [environment, tenantId])

  function selectDefault(type: 'chat' | 'embedding' | 'rerank', modelId: string) {
    setDefaultModelIds((current) => ({ ...current, [type]: modelId || null }))
    if (modelId) setEnabledModelIds((current) => current.includes(modelId) ? current : [...current, modelId])
  }

  async function saveConfiguration() {
    if (!config || !canEdit) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const availableIds = new Set(config.models.map((model) => model.id))
      const enabled = enabledModelIds.filter((id) => availableIds.has(id))
      const saved = await apiRequest<TenantModelEnvironment>(
        `/api/v1/tenant/model-environments/${environment}`,
        {
          method: 'PUT',
          body: JSON.stringify({
            expected_version: config.version,
            enabled_model_ids: enabled,
            default_model_ids: defaultModelIds,
          }),
        },
        tenantId,
      )
      setConfig(saved)
      setEnabledModelIds(saved.enabled_model_ids)
      setNotice('模型环境配置已保存。')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存模型配置失败')
    } finally {
      setSaving(false)
    }
  }

  function changeEnvironment(nextEnvironment: TenantModelEnvironment['environment']) {
    if (nextEnvironment === environment) return
    setLoading(true)
    setConfig(null)
    setEnabledModelIds([])
    setDefaultModelIds({ chat: null, embedding: null, rerank: null })
    setError('')
    setNotice('')
    setEnvironment(nextEnvironment)
  }

  const [testModelId, setTestModelId] = useState('')
  const [gatewayBusy, setGatewayBusy] = useState(false)

  async function resyncGateway() {
    setGatewayBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await apiRequest<TenantModelEnvironment>(`/api/v1/tenant/model-environments/${environment}/sync`, { method: 'POST' }, tenantId)
      setConfig(result)
      setNotice(result.gateway_sync_status === 'synced' ? '已同步到 AI 网关。' : `同步未完成：${result.gateway_error ?? 'AI 网关未配置'}`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '同步失败')
    } finally {
      setGatewayBusy(false)
    }
  }

  async function testModel(accessOnly: boolean) {
    if (!testModelId) return
    setGatewayBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await apiRequest<ModelTestResult>(
        `/api/v1/tenant/model-environments/${environment}/test`,
        { method: 'POST', body: JSON.stringify({ model_id: testModelId, access_only: accessOnly }) },
        tenantId,
      )
      if (result.ok) setNotice(`调用成功${result.access_only ? '（仅校验网关凭证与模型权限，未调用供应商）' : ''}，耗时 ${result.latency_ms} ms：${result.content ?? ''}`)
      else setError(`调用失败：${result.error ?? '未知错误'}`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '测试失败')
    } finally {
      setGatewayBusy(false)
    }
  }

  const enabledSet = new Set(enabledModelIds)
  const staleCount = config?.unavailable_enabled_model_ids.length ?? 0
  return (
    <section className="tenant-model-settings">
      <div className="page-heading settings-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />企业设置 · 模型与环境</div>
          <h1>模型与环境</h1>
          <p>按环境选择可用模型，并为对话、向量化和重排序指定默认模型。</p>
        </div>
        <div className="settings-region-stamp">
          <span>{config ? enumLabel('plan', config.plan) : '套餐核验中'}</span>
          <strong>{config ? enumLabel('region', config.region) : '读取区域…'}</strong>
        </div>
      </div>
      <div className="model-environment-tabs" role="tablist" aria-label="选择配置环境">
        {([
          ['development', '开发'],
          ['staging', '预发'],
          ['production', '生产'],
        ] as const).filter(([value]) => !config || config.available_environments.includes(value)).map(([value, label]) => (
          <button
            aria-selected={environment === value}
            className={environment === value ? 'active' : ''}
            key={value}
            onClick={() => changeEnvironment(value)}
            role="tab"
            type="button"
          >
            <i />{label}
          </button>
        ))}
        <span className="model-environment-version">
          {config ? `配置版本 ${config.version || '尚未保存'}` : '环境配置'}
          {config?.updated_at ? ` · ${new Date(config.updated_at).toLocaleDateString('zh-CN')}` : ''}
        </span>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      {staleCount > 0 && (
        <p className="model-environment-warning" role="status">
          有 {staleCount} 个已启用模型已不符合当前套餐、区域或目录状态；保存时会从本环境移除，请重新选择默认模型。
        </p>
      )}
      {config && config.version > 0 && (
        <div className="panel gateway-status-panel">
          <div className="members-table-heading">
            <div><div className="section-kicker">AI 网关</div><h2>本环境调用凭证</h2></div>
            <span className={`member-status ${config.gateway_sync_status === 'synced' ? 'active' : 'inactive'}`}><i />{enumLabel('gatewaySync', config.gateway_sync_status)}</span>
          </div>
          <p className="model-settings-help">保存配置后，平台会在 AI 网关中为本环境签发独立的调用凭证，并只放行已启用的模型。{config.gateway_error && ` 最近错误：${config.gateway_error}`}</p>
          {canEdit && (
            <div className="gateway-test-row">
              <button className="tenant-status-action" disabled={gatewayBusy} onClick={() => void resyncGateway()} type="button">重新同步</button>
              {config.gateway_sync_status === 'synced' && (
                <>
                  <select aria-label="选择测试模型" onChange={(event) => setTestModelId(event.target.value)} value={testModelId}>
                    <option value="">选择已启用的对话模型</option>
                    {config.models.filter((model) => model.model_type === 'chat' && config.enabled_model_ids.includes(model.id)).map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
                  </select>
                  <button className="tenant-status-action" disabled={gatewayBusy || !testModelId} onClick={() => void testModel(true)} type="button">链路检查</button>
                  <button className="tenant-status-action" disabled={gatewayBusy || !testModelId} onClick={() => void testModel(false)} type="button">真实调用</button>
                </>
              )}
            </div>
          )}
        </div>
      )}
      <div className="panel model-defaults-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">默认路由</div><h2>本环境默认模型</h2></div>
          <span className="model-environment-count">{enabledModelIds.filter((id) => config?.models.some((model) => model.id === id)).length} 个已启用</span>
        </div>
        <div className="model-default-grid">
          {(['chat', 'embedding', 'rerank'] as const).map((type) => {
            const options = config?.models.filter((model) => model.model_type === type && enabledSet.has(model.id)) ?? []
            return (
              <label key={type}>
                <span>{enumLabel('modelTypeLong', type)}默认模型</span>
                <select
                  disabled={!canEdit || loading || options.length === 0}
                  onChange={(event) => selectDefault(type, event.target.value)}
                  value={defaultModelIds[type] ?? ''}
                >
                  <option value="">{options.length ? '不设置默认模型' : '无可用模型'}</option>
                  {options.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
                </select>
              </label>
            )
          })}
        </div>
        <p className="model-settings-help">默认模型必须同时满足套餐、数据区域和模型类型要求，并且已在当前环境启用。</p>
      </div>
      <div className="panel tenant-model-list-panel">
        <div className="members-table-heading">
          <div><div className="section-kicker">平台模型目录</div><h2>本环境可用模型</h2></div>
          <span className="model-environment-count">{config?.models.length ?? 0} 个符合条件</span>
        </div>
        {loading ? <div className="members-empty">正在读取当前环境可用模型…</div> : !config?.models.length ? (
          <div className="model-catalog-empty">
            <strong>当前没有可用模型</strong>
            <p>请联系平台管理员检查模型目录、套餐开放范围和数据区域。</p>
          </div>
        ) : (
          <div className="tenant-model-list">
            {config.models.map((model) => {
              const enabled = enabledSet.has(model.id)
              const isDefault = Object.values(defaultModelIds).includes(model.id)
              return (
                <label className={`tenant-model-row${enabled ? ' enabled' : ''}`} key={model.id}>
                  <input
                    checked={enabled}
                    disabled={!canEdit}
                    onChange={(event) => {
                      setEnabledModelIds((current) => event.target.checked ? [...current, model.id] : current.filter((id) => id !== model.id))
                      if (!event.target.checked) {
                        setDefaultModelIds((current) => ({
                          chat: current.chat === model.id ? null : current.chat,
                          embedding: current.embedding === model.id ? null : current.embedding,
                          rerank: current.rerank === model.id ? null : current.rerank,
                        }))
                      }
                    }}
                    type="checkbox"
                  />
                  <span className="tenant-model-mark"><Bot size={17} /></span>
                  <span className="tenant-model-copy">
                    <strong>{model.display_name}{isDefault && <em>默认</em>}</strong>
                    <small>{model.provider} · {enumLabel('modelTypeLong', model.model_type)} · {enumLabel('region', model.region)}</small>
                  </span>
                  <span className="tenant-model-price">输入 ¥{model.input_price_per_million} / 1M tokens</span>
                  <span className={`tenant-model-state${enabled ? ' on' : ''}`}>{enabled ? '已启用' : '未启用'}</span>
                </label>
              )
            })}
          </div>
        )}
        <div className="model-settings-footer">
          <span>{canEdit ? '修改后保存仅影响当前环境。' : '你有权查看配置，但没有修改权限。'}</span>
          {canEdit && <button className="primary-button" disabled={loading || saving || !config} onClick={() => void saveConfiguration()} type="button">{saving ? '正在保存…' : '保存当前环境'}</button>}
        </div>
      </div>
      <p className="model-settings-security">上游密钥仅由平台服务端管理，不会通过租户设置接口返回。BYOK 和跨环境发布将在密钥管理及审批能力完成后接入。</p>
    </section>
  )
}
