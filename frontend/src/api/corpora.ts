import { fetchJson } from '@/api/client'

export type CorpusRead = {
  id: string
  name: string
  inbox_path: string
  rules_path: string | null
  created_at: string | null
}

export function listCorpora(): Promise<CorpusRead[]> {
  return fetchJson('/api/corpora')
}
