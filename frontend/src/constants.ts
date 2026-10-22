

export const PLAN_LABELS: Record<string, string> = {
  standard: '标准版',
  professional: '专业版',
  enterprise: '企业版',
}

export const REGION_LABELS: Record<string, string> = {
  us_east: 'US-East',
  eu_west: 'EU-West',
  ap_singapore: 'AP-Singapore',
  cn_shanghai: 'CN-Shanghai',
}

export const MODEL_TYPE_LABELS: Record<string, string> = {
  chat: '对话与推理',
  embedding: '向量化',
  rerank: '重排序',
  vision: '视觉',
  speech: '语音',
}

export const MODEL_PROVIDERS: Record<string, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  azure_openai: 'Azure OpenAI',
  alibaba_qwen: '阿里云 Qwen',
  private: '私有部署',
}

export const MODEL_TYPES: Record<string, string> = {
  chat: '对话',
  embedding: '向量',
  rerank: '重排',
  vision: '视觉',
  speech: '语音',
}

export const MODEL_AVAILABILITY: Record<string, string> = {
  all_plans: '全部套餐',
  professional_plus: '专业版及以上',
  enterprise: '仅企业版',
  allowlist: '白名单租户',
}

export const MODEL_REGIONS: Record<string, string> = {
  us_east: '美国东部',
  eu_west: '欧洲西部',
  ap_singapore: '亚太新加坡',
  cn_shanghai: '中国上海',
}

export const TENANT_STATUS_LABELS: Record<string, string> = {
  trial: '试用',
  active: '正式',
  grace: '宽限',
  read_only: '只读',
  suspended: '停用',
  pending_deletion: '待删除',
  deleted: '已删除',
}

export const GATEWAY_SYNC_LABELS: Record<string, string> = {
  not_configured: '网关未配置',
  synced: '已同步',
  failed: '同步失败',
}
