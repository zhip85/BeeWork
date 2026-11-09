export type Industry = { code: string; name_zh: string; name_en: string; sort_order: number; status: 'active' | 'disabled'; tenant_count: number; glossary_count: number }
export type Glossary = {
  id: string
  name: string
  description: string
  industry_code: string
  industry_name: string
  term_count: number
  tenant_count: number
  row_version: number
  updated_at: string
}
export type Term = { id: string; glossary_id: string; term: string; aliases: string[]; definition: string; row_version: number; updated_by: string | null; updated_at: string }
export type TermList = { items: Term[]; total: number; limit: number; offset: number }
export type ImportResult = {
  dry_run: boolean
  recognized: number
  duplicates: number
  created: number
  updated: number
  skipped: number
  errors: { line: number; message: string }[]
  preview: { line: number; term: string; aliases: string[]; definition: string; exists: boolean }[]
}
