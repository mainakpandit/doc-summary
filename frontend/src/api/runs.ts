import { fetchJson } from '@/api/client'

export type RunKind = 'initial' | 'update'

export type RunStatus =
  | 'pending'
  | 'running'
  | 'awaiting_review'
  | 'committing'
  | 'done'
  | 'failed'
  | 'cancelled'

export const TERMINAL_RUN_STATUSES: readonly RunStatus[] = ['done', 'failed', 'cancelled']

export type RunRead = {
  id: string
  corpus_id: string
  kind: RunKind
  status: RunStatus
  parent_run_id: string | null
  triggering_document_id: string | null
  started_at: string | null
  completed_at: string | null
  error: string | null
  idempotency_key: string | null
}

export type RunDetail = RunRead & {
  current_stage: string | null
  counts: { claims: number; conflicts: number; findings: number }
}

export type CostStage = {
  stage: string
  calls: number
  input_tokens: number
  output_tokens: number
  latency_ms: number
  usd_cost: number
}

export type RunCost = {
  run_id: string
  total_usd_cost: number
  stages: CostStage[]
}

export type AuditEvent = {
  id: number
  run_id: string | null
  event_type: string
  payload: Record<string, unknown>
  occurred_at: string | null
}

export function listRuns(): Promise<RunRead[]> {
  return fetchJson('/api/runs')
}

export function getRun(runId: string): Promise<RunDetail> {
  return fetchJson(`/api/runs/${runId}`)
}

export function getRunCost(runId: string): Promise<RunCost> {
  return fetchJson(`/api/runs/${runId}/cost`)
}
