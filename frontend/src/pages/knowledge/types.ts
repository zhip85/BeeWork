export type KbType = 'standard' | 'faq'
export type KbStatus = 'draft' | 'processing' | 'indexed'
export type SyncStatus = 'pending' | 'synced' | 'failed' | 'not_configured'

export type KnowledgeBase = {
  id: string
  name: string
  description: string
  kb_type: KbType
  language: 'multilingual' | 'zh-CN' | 'en'
  embedding_model: 'bge-m3' | 'text-embedding-3-large' | 'cohere-embed-v3'
  chunking: 'semantic' | 'fixed_512' | 'by_heading'
  retrieval_threshold: string
  organization_id: string
  organization_name: string
  status: KbStatus
  docs_count: number
  faq_count: number
  sync_status: SyncStatus
  sync_error: string | null
  reindexing: boolean
  row_version: number
  updated_at: string
}

export type KnowledgePage = { items: KnowledgeBase[]; total: number }
