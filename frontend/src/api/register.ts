import { fetchJson } from '@/api/client'
import type { Citation } from '@/api/reviews'

export type RegisterFields = {
  name: string | null
  owner: string | null
  target_release: string | null
  status: string | null
  open_risks: string[]
} & Record<string, unknown>

export type FieldClaim = {
  claim_id: string
  predicate: string
  object: string
  confidence: number
  sources: Citation[]
}

export type RegisterEntry = {
  id: string
  feature_key: string
  fields: RegisterFields
  field_claims: Record<string, FieldClaim[]>
  version: number
  updated_at: string | null
}

export function getRegister(corpusId: string): Promise<RegisterEntry[]> {
  return fetchJson(`/api/corpora/${corpusId}/register`)
}
