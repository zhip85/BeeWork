import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Boxes,
  Plus,
  X,
} from 'lucide-react'
import { apiRequest } from '../../api'
import { GATEWAY_SYNC_LABELS, MODEL_AVAILABILITY, MODEL_PROVIDERS, MODEL_REGIONS, MODEL_TYPES } from '../../constants'
import type { ModelDraft, ModelTestResult, PlatformModel, PlatformTenant } from '../../types'

function emptyModelDraft(): ModelDraft {
  return {
    id: null,
    display_name: '',
    provider: 'openai',
    model_type: 'chat',
    availability: 'all_plans',
    input_price_per_million: '0',
    output_price_per_million: '0',
    input_points_per_thousand: '0',
    output_points_per_thousand: '0',
    status: 'healthy',
    provider_model_id: '',
    endpoint_url: '',
    api_key: '',
    api_version: '',
    region: 'us_east',
    context_window: '',
    max_output_tokens: '',
    default_temperature: '',
    rpm_limit: '',
    fallback_model_id: '',
    allowlist_tenant_ids: [],
  }
}

export function PlatformModelsPage() {
  const [models, setModels] = useState<PlatformModel[]>([])
  const [tenants, setTenants] = useState<PlatformTenant[]>([])
  const [draft, setDraft] = useState<ModelDraft | null>(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function refreshModels() {
    const [modelList, tenantList] = await Promise.all([
      apiRequest<{ models: PlatformModel[] }>('/api/v1/platform/models'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
    ])
    setModels(modelList.models)
    setTenants(tenantList)
  }

  useEffect(() => {
    let active = true
    Promise.all([
      apiRequest<{ models: PlatformModel[] }>('/api/v1/platform/models'),
      apiRequest<PlatformTenant[]>('/api/v1/platform/tenants'),
    ])
      .then(([modelList, tenantList]) => {
        if (!active) return
        setModels(modelList.models)
        setTenants(tenantList)
      })
      .catch((requestError: Error) => active && setError(requestError.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  function editModel(model: PlatformModel) {
    setError('')
    setNotice('')
    setDraft({
      id: model.id,
      display_name: model.display_name,
      provider: model.provider,
      model_type: model.model_type,
      availability: model.availability,
      input_price_per_million: model.input_price_per_million,
      output_price_per_million: model.output_price_per_million,
      input_points_per_thousand: model.input_points_per_thousand,
      output_points_per_thousand: model.output_points_per_thousand,
      status: model.status,
      provider_model_id: model.provider_model_id,
      endpoint_url: model.endpoint_url,
      api_key: '',
      api_version: model.api_version ?? '',
      region: model.region,
      context_window: model.context_window?.toString() ?? '',
      max_output_tokens: model.max_output_tokens?.toString() ?? '',
      default_temperature: model.default_temperature ?? '',
      rpm_limit: model.rpm_limit?.toString() ?? '',
      fallback_model_id: model.fallback_model_id ?? '',
      allowlist_tenant_ids: [...model.allowlist_tenant_ids],
    })
  }

  function updateDraft<K extends keyof ModelDraft>(key: K, value: ModelDraft[K]) {
    setDraft((current) => current ? { ...current, [key]: value } : current)
  }

  async function saveModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draft) return
    setSaving(true)
    setError('')
    setNotice('')
    const payload = {
      ...draft,
      api_key: draft.api_key.trim() || null,
      context_window: draft.context_window ? Number(draft.context_window) : null,
      max_output_tokens: draft.max_output_tokens ? Number(draft.max_output_tokens) : null,
      default_temperature: draft.default_temperature || null,
      rpm_limit: draft.rpm_limit ? Number(draft.rpm_limit) : null,
      fallback_model_id: draft.fallback_model_id || null,
    }
    try {
      const saved = await apiRequest<PlatformModel>(
        draft.id ? `/api/v1/platform/models/${draft.id}` : '/api/v1/platform/models',
        {
          method: draft.id ? 'PUT' : 'POST',
          body: JSON.stringify(payload),
        },
      )
      await refreshModels()
      setDraft(null)
      setNotice(draft.id ? `已更新模型「${saved.display_name}」。` : `已新增模型「${saved.display_name}」。`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '保存模型失败')
    } finally {
      setSaving(false)
    }
  }

  const visibleModels = models.filter((model) => {
    const textMatches = `${model.display_name} ${model.provider_model_id} ${MODEL_PROVIDERS[model.provider] ?? model.provider}`
      .toLowerCase()
      .includes(search.trim().toLowerCase())
    return textMatches && (!statusFilter || model.status === statusFilter)
  })

  const [busyModel, setBusyModel] = useState('')

  async function resyncModel(model: PlatformModel) {
    setBusyModel(model.id)
    setError('')
    setNotice('')
    try {
      const updated = await apiRequest<PlatformModel>(`/api/v1/platform/models/${model.id}/sync`, { method: 'POST' })
      setModels((current) => current.map((item) => item.id === updated.id ? updated : item))
      setNotice(updated.gateway_sync_status === 'synced' ? `${model.display_name} 已同步到 AI 网关。` : `同步未完成：${updated.gateway_error ?? 'AI 网关未配置'}`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '同步失败')
    } finally {
      setBusyModel('')
    }
  }

  async function testModel(model: PlatformModel, accessOnly: boolean) {
    setBusyModel(model.id)
    setError('')
    setNotice('')
    try {
      const result = await apiRequest<ModelTestResult>(`/api/v1/platform/models/${model.id}/test`, { method: 'POST', body: JSON.stringify({ access_only: accessOnly }) })
      if (result.ok) {
        setNotice(`${model.display_name}${result.access_only ? '（仅校验网关凭证与模型权限，未调用供应商）' : ''} 调用成功，耗时 ${result.latency_ms} ms：${result.content ?? ''}`)
      } else {
        setError(`${model.display_name} 调用失败：${result.error ?? '未知错误'}`)
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '测试失败')
    } finally {
      setBusyModel('')
    }
  }

  return (
    <section className="platform-models-page">
      <div className="platform-page-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" />平台定义 · AI 网关</div>
          <h1>模型目录</h1>
          <p>管理平台可供租户使用的模型、区域、价格和凭证。密钥加密保存，不会再次显示明文。</p>
        </div>
        <button className="primary-button" onClick={() => { setError(''); setDraft(emptyModelDraft()) }} type="button">
          <Plus size={16} />新增模型
        </button>
      </div>
      {notice && <p className="tenant-success" role="status">{notice}</p>}
      {error && <p className="members-error" role="alert">{error}</p>}
      <div className="model-catalog-toolbar">
        <label className="model-search">搜索模型
          <input onChange={(event) => setSearch(event.target.value)} placeholder="模型名称、供应商或模型 ID" value={search} />
        </label>
        <label>状态
          <select onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}>
            <option value="">全部状态</option>
            <option value="healthy">健康</option>
            <option value="canary">灰度发布</option>
            <option value="disabled">已停用</option>
          </select>
        </label>
        <span className="model-catalog-count">{visibleModels.length} 个模型</span>
      </div>
      <div className="panel model-catalog-panel">
        {loading ? <div className="members-empty">正在读取模型目录…</div> : visibleModels.length === 0 ? (
          <div className="model-catalog-empty">
            <span className="model-empty-mark"><Boxes size={22} /></span>
            <strong>{models.length === 0 ? '模型目录尚未配置' : '没有符合条件的模型'}</strong>
            <p>{models.length === 0 ? '添加一个受支持的模型后，再配置其套餐范围、区域与凭证。' : '调整搜索条件或状态筛选。'}</p>
          </div>
        ) : (
          <div className="members-table-wrap">
            <table className="members-table model-catalog-table">
              <thead><tr><th>模型</th><th>类型与区域</th><th>开放范围</th><th>输入 / 输出价格</th><th>凭证</th><th>状态</th><th>AI 网关</th><th>操作</th></tr></thead>
              <tbody>{visibleModels.map((model) => (
                <tr key={model.id}>
                  <td><span className="model-name-cell"><strong>{model.display_name}</strong><small>{MODEL_PROVIDERS[model.provider] ?? model.provider} · {model.provider_model_id}</small></span></td>
                  <td>{MODEL_TYPES[model.model_type] ?? model.model_type}<small className="platform-tenant-slug">{MODEL_REGIONS[model.region] ?? model.region}</small></td>
                  <td>{MODEL_AVAILABILITY[model.availability] ?? model.availability}{model.availability === 'allowlist' && <small className="platform-tenant-slug">{model.allowlist_tenant_ids.length} 家企业</small>}</td>
                  <td><span className="model-price-cell">${model.input_price_per_million} / ${model.output_price_per_million}<small>每百万 Token</small></span></td>
                  <td><span className="model-key-mask">{model.credential_configured ? model.api_key_masked : '未配置'}</span></td>
                  <td><span className={`member-status ${model.status === 'healthy' ? 'active' : 'inactive'}`}><i />{model.status === 'healthy' ? '健康' : model.status === 'canary' ? '灰度发布' : '已停用'}</span></td>
                  <td><span className={`member-status ${model.gateway_sync_status === 'synced' ? 'active' : 'inactive'}`} title={model.gateway_error ?? undefined}><i />{GATEWAY_SYNC_LABELS[model.gateway_sync_status] ?? model.gateway_sync_status}</span></td>
                  <td className="member-actions-cell">
                    <button className="text-action" onClick={() => editModel(model)} type="button">编辑</button>
                    <button className="text-action" disabled={busyModel === model.id} onClick={() => void resyncModel(model)} type="button">同步</button>
                    {model.gateway_sync_status === 'synced' && model.model_type === 'chat' && model.status !== 'disabled' && (
                      <>
                        <button className="text-action" disabled={busyModel === model.id} onClick={() => void testModel(model, true)} type="button">链路检查</button>
                        <button className="text-action" disabled={busyModel === model.id} onClick={() => void testModel(model, false)} type="button">真实调用</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
      {draft && (
        <div className="modal-backdrop model-modal-backdrop" role="presentation" onClick={() => !saving && setDraft(null)}>
          <form className="model-editor" onSubmit={saveModel} role="dialog" aria-modal="true" aria-labelledby="model-editor-title" onClick={(event) => event.stopPropagation()}>
            <header className="model-editor-header">
              <div><div className="section-kicker">AI 网关 · 模型注册</div><h2 id="model-editor-title">{draft.id ? '编辑模型' : '新增模型'}</h2></div>
              <button className="icon-button" aria-label="关闭" disabled={saving} onClick={() => setDraft(null)} type="button"><X size={18} /></button>
            </header>
            <div className="model-editor-scroll">
              <div className="model-editor-section">
                <strong>模型身份</strong>
                <div className="model-editor-grid">
                  <label>显示名称<input maxLength={120} onChange={(event) => updateDraft('display_name', event.target.value)} required value={draft.display_name} /></label>
                  <label>供应商<select onChange={(event) => updateDraft('provider', event.target.value)} value={draft.provider}>{Object.entries(MODEL_PROVIDERS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>模型类型<select onChange={(event) => updateDraft('model_type', event.target.value)} value={draft.model_type}>{Object.entries(MODEL_TYPES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>供应商模型 ID<input maxLength={160} onChange={(event) => updateDraft('provider_model_id', event.target.value)} required value={draft.provider_model_id} /></label>
                  <label className="model-editor-wide">HTTPS 接口地址<input onChange={(event) => updateDraft('endpoint_url', event.target.value)} placeholder="https://api.provider.com/v1" required type="url" value={draft.endpoint_url} /></label>
                  <label>API 版本<input maxLength={80} onChange={(event) => updateDraft('api_version', event.target.value)} placeholder="Azure 等供应商可填写" value={draft.api_version} /></label>
                  <label>部署区域<select onChange={(event) => updateDraft('region', event.target.value)} value={draft.region}>{Object.entries(MODEL_REGIONS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                </div>
              </div>
              <div className="model-editor-section">
                <strong>租户开放与供给</strong>
                <div className="model-editor-grid">
                  <label>开放范围<select onChange={(event) => updateDraft('availability', event.target.value)} value={draft.availability}>{Object.entries(MODEL_AVAILABILITY).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
                  <label>目录状态<select onChange={(event) => updateDraft('status', event.target.value as PlatformModel['status'])} value={draft.status}><option value="healthy">健康</option><option value="canary">灰度发布</option><option value="disabled">已停用</option></select></label>
                  {draft.availability === 'allowlist' && <fieldset className="model-allowlist"><legend>白名单租户</legend>{tenants.map((tenant) => <label key={tenant.id}><input checked={draft.allowlist_tenant_ids.includes(tenant.id)} onChange={(event) => updateDraft('allowlist_tenant_ids', event.target.checked ? [...draft.allowlist_tenant_ids, tenant.id] : draft.allowlist_tenant_ids.filter((id) => id !== tenant.id))} type="checkbox" />{tenant.name}</label>)}</fieldset>}
                  <label className="model-editor-wide">API Key<input autoComplete="new-password" onChange={(event) => updateDraft('api_key', event.target.value)} placeholder={draft.id ? '留空以保留当前密钥；输入新值则轮换' : '保存后只显示掩码'} required={!draft.id} type="password" value={draft.api_key} /><small>{draft.id ? '明文不会回显；留空不修改现有凭证。' : '凭证会加密保存，API 不会返回明文。'}</small></label>
                </div>
              </div>
              <div className="model-editor-section">
                <strong>价格与运行限制</strong>
                <div className="model-editor-grid">
                  <label>输入成本 / 百万 Token<input min="0" onChange={(event) => updateDraft('input_price_per_million', event.target.value)} required step="0.000001" type="number" value={draft.input_price_per_million} /></label>
                  <label>输出成本 / 百万 Token<input min="0" onChange={(event) => updateDraft('output_price_per_million', event.target.value)} required step="0.000001" type="number" value={draft.output_price_per_million} /></label>
                  <label>输入扣点 / 千 Token<input min="0" onChange={(event) => updateDraft('input_points_per_thousand', event.target.value)} required step="0.000001" type="number" value={draft.input_points_per_thousand} /></label>
                  <label>输出扣点 / 千 Token<input min="0" onChange={(event) => updateDraft('output_points_per_thousand', event.target.value)} required step="0.000001" type="number" value={draft.output_points_per_thousand} /></label>
                  <label>上下文窗口<input min="1" onChange={(event) => updateDraft('context_window', event.target.value)} type="number" value={draft.context_window} /></label>
                  <label>最大输出 Token<input min="1" onChange={(event) => updateDraft('max_output_tokens', event.target.value)} type="number" value={draft.max_output_tokens} /></label>
                  <label>默认温度<input max="2" min="0" onChange={(event) => updateDraft('default_temperature', event.target.value)} step="0.01" type="number" value={draft.default_temperature} /></label>
                  <label>平台 RPM 上限<input min="1" onChange={(event) => updateDraft('rpm_limit', event.target.value)} type="number" value={draft.rpm_limit} /></label>
                  <label className="model-editor-wide">降级模型<select onChange={(event) => updateDraft('fallback_model_id', event.target.value)} value={draft.fallback_model_id}><option value="">不配置降级模型</option>{models.filter((model) => model.id !== draft.id && model.model_type === draft.model_type && model.region === draft.region && model.status !== 'disabled').map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}</select><small>只能选择同类型、同区域模型；服务端会阻止循环引用。</small></label>
                </div>
              </div>
            </div>
            <footer className="model-editor-footer">
              <button className="secondary-button" disabled={saving} onClick={() => setDraft(null)} type="button">取消</button>
              <button className="primary-button" disabled={saving} type="submit">{saving ? '正在保存…' : '保存模型'}</button>
            </footer>
          </form>
        </div>
      )}
    </section>
  )
}
