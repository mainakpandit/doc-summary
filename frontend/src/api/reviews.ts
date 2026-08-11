import { fetchJson } from '@/api/client'

export type Citation = {
  chunk_id: string
  document_id: string
  document_filename: string
  page: number | null
  quote: string
  char_start: number
  char_end: number
  snippet: string
  highlight_start: number
  highlight_end: number
}

export type ConflictClaim = {
  id: string
  object: string
  confidence: number
  sources: Citation[]
}

export type ConflictItem = {
  id: string
  item_type: 'conflict'
  subject: string
  predicate: string
  claim_a: ConflictClaim
  claim_b: ConflictClaim
}

export type FindingItem = {
  id: string
  item_type: 'finding'
  rule_id: string
  severity: 'info' | 'warning' | 'error'
  subject: string | null
  message: string
  sources: Citation[]
}

export type RegisterChangeItem = {
  id: string
  item_type: 'register_change'
  change_kind: 'addition' | 'field_change'
  feature_key: string
  fields?: Record<string, unknown>
  field_name?: string
  old_value?: unknown
  new_value?: unknown
  sources: Citation[]
}

export type ReviewPayload = {
  run_id: string
  status: string
  conflicts: ConflictItem[]
  findings: FindingItem[]
  register_changes: RegisterChangeItem[]
}

export type ReviewItemType = ConflictItem['item_type'] | FindingItem['item_type'] | RegisterChangeItem['item_type']
export type ReviewDecision = 'approve' | 'reject'

export type ReviewItemDecision = {
  id: string
  item_type: ReviewItemType
  decision: ReviewDecision
  note?: string
}

export function getReview(runId: string): Promise<ReviewPayload> {
  return fetchJson(`/api/runs/${runId}/review`)
}

export function submitReview(
  runId: string,
  items: ReviewItemDecision[],
  reviewer: string,
): Promise<{ accepted: number }> {
  return fetchJson(`/api/runs/${runId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Reviewer': reviewer },
    body: JSON.stringify({ items, reviewer }),
  })
}

export function resumeRun(runId: string): Promise<{ run_id: string; status: string }> {
  return fetchJson(`/api/runs/${runId}/resume`, { method: 'POST' })
}
