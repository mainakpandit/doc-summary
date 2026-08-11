import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { ChevronDown, ChevronRight, Circle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { listCorpora } from '@/api/corpora'
import { getRun, type RunStatus } from '@/api/runs'
import { connectSse, type SseEvent, type SseStatus } from '@/api/sse'
import { cn } from '@/lib/utils'

const POLL_MS = 3000

// Mirrors the node names `agent/graph.py` registers with `instrument()`,
// in graph order, so pending stages render before their events arrive.
const STAGE_ORDER = [
  'classify',
  'classify_review',
  'extract',
  'detect_conflicts',
  'examine',
  'build_register',
  'human_gate',
  'commit',
  'finish',
] as const

type StageStatus = 'pending' | 'running' | 'done' | 'failed'

type StageState = {
  name: string
  status: StageStatus
  startPayload?: unknown
  endPayload?: unknown
}

const STAGE_EVENT_TYPES = STAGE_ORDER.flatMap((name) => [`${name}_start`, `${name}_end`])
const TERMINAL_EVENT_TYPES = ['run_completed', 'run_failed']
const EVENT_TYPES = [...STAGE_EVENT_TYPES, ...TERMINAL_EVENT_TYPES]

const STAGE_STATUS_CLASSES: Record<StageStatus, string> = {
  pending: 'bg-secondary text-secondary-foreground',
  running: 'bg-primary text-primary-foreground',
  done: 'bg-emerald-600 text-white hover:bg-emerald-600',
  failed: 'bg-destructive text-white',
}

const RUN_STATUS_CLASSES: Record<RunStatus, string> = {
  pending: 'bg-secondary text-secondary-foreground',
  running: 'bg-primary text-primary-foreground',
  committing: 'bg-primary text-primary-foreground',
  awaiting_review: 'bg-amber-500 text-white hover:bg-amber-500',
  done: 'bg-emerald-600 text-white hover:bg-emerald-600',
  failed: 'bg-destructive text-white',
  cancelled: 'bg-muted text-muted-foreground',
}

function initialStages(): Record<string, StageState> {
  return Object.fromEntries(
    STAGE_ORDER.map((name) => [name, { name, status: 'pending' as StageStatus }]),
  )
}

function humanize(name: string): string {
  return name
    .split('_')
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ')
}

function StageCard({ stage }: { stage: StageState }) {
  const [expanded, setExpanded] = useState(false)
  const canExpand = stage.status === 'done'

  return (
    <Card>
      <CardHeader
        className={cn(canExpand && 'cursor-pointer select-none')}
        onClick={canExpand ? () => setExpanded((v) => !v) : undefined}
      >
        <CardTitle className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            {canExpand ? (
              expanded ? (
                <ChevronDown className="size-4 text-muted-foreground" />
              ) : (
                <ChevronRight className="size-4 text-muted-foreground" />
              )
            ) : (
              <Circle className="size-3 text-transparent" />
            )}
            {humanize(stage.name)}
          </span>
          <Badge className={STAGE_STATUS_CLASSES[stage.status]}>{stage.status}</Badge>
        </CardTitle>
      </CardHeader>
      {canExpand && expanded ? (
        <CardContent>
          <pre className="overflow-x-auto rounded-lg bg-muted p-4 text-xs text-muted-foreground">
            {JSON.stringify(
              { start: stage.startPayload, end: stage.endPayload },
              null,
              2,
            )}
          </pre>
        </CardContent>
      ) : null}
    </Card>
  )
}

export function RunDetail() {
  const { id } = useParams<{ id: string }>()
  const [stages, setStages] = useState<Record<string, StageState>>(initialStages)
  const [sseStatus, setSseStatus] = useState<SseStatus>('connecting')

  const runQuery = useQuery({
    queryKey: ['run', id],
    queryFn: () => getRun(id!),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'done' || status === 'failed' || status === 'cancelled'
        ? false
        : POLL_MS
    },
  })

  const corporaQuery = useQuery({
    queryKey: ['corpora'],
    queryFn: listCorpora,
    staleTime: 60_000,
  })

  const corpusName = useMemo(() => {
    const corpusId = runQuery.data?.corpus_id
    return corporaQuery.data?.find((c) => c.id === corpusId)?.name ?? corpusId
  }, [runQuery.data?.corpus_id, corporaQuery.data])

  useEffect(() => {
    if (!id) return
    setStages(initialStages())

    // `conn` is assigned before any event can fire (EventSource callbacks
    // are always async), so the closure below can safely call `conn.close()`
    // itself once a terminal event arrives -- otherwise the server closing
    // an already-finished run's stream cleanly looks like a disconnect and
    // this wrapper would reconnect forever just to replay the same history.
    const conn = connectSse(
      `/api/runs/${id}/events`,
      EVENT_TYPES,
      (event: SseEvent) => {
        if ((TERMINAL_EVENT_TYPES as string[]).includes(event.type)) {
          conn.close()
          return
        }

        const match = event.type.match(/^(.*)_(start|end)$/)
        if (!match) return
        const [, name, phase] = match
        if (!STAGE_ORDER.includes(name as (typeof STAGE_ORDER)[number])) return

        setStages((prev) => {
          const existing = prev[name] ?? { name, status: 'pending' as StageStatus }
          return {
            ...prev,
            [name]:
              phase === 'start'
                ? { ...existing, status: 'running', startPayload: event.data }
                : { ...existing, status: 'done', endPayload: event.data },
          }
        })
      },
      setSseStatus,
    )

    return () => conn.close()
  }, [id])

  if (!id) return null

  const run = runQuery.data
  const runFailed = run?.status === 'failed' || run?.status === 'cancelled'

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div>
        <p className="text-sm text-muted-foreground">Run</p>
        <h1 className="font-mono text-xl font-bold text-foreground">{id}</h1>
      </div>

      {runQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading run…</p>
      ) : runQuery.isError ? (
        <p className="text-sm text-destructive">{(runQuery.error as Error).message}</p>
      ) : run ? (
        <>
          <Card>
            <CardContent className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
              <div>
                <p className="text-muted-foreground">Corpus</p>
                <p className="text-foreground">{corpusName}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Kind</p>
                <p className="text-foreground">{run.kind}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Status</p>
                <Badge className={RUN_STATUS_CLASSES[run.status]}>{run.status}</Badge>
              </div>
              <div>
                <p className="text-muted-foreground">Started</p>
                <p className="text-foreground">
                  {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">Completed</p>
                <p className="text-foreground">
                  {run.completed_at ? new Date(run.completed_at).toLocaleString() : '—'}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">Claims / Conflicts / Findings</p>
                <p className="text-foreground">
                  {run.counts.claims} / {run.counts.conflicts} / {run.counts.findings}
                </p>
              </div>
              {run.error ? (
                <div className="col-span-full">
                  <p className="text-muted-foreground">Error</p>
                  <p className="text-destructive">{run.error}</p>
                </div>
              ) : null}
            </CardContent>
          </Card>

          {run.status === 'awaiting_review' ? (
            <Card className="border-amber-500 bg-amber-500/10">
              <CardContent className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <p className="font-semibold text-foreground">Awaiting review</p>
                  <p className="text-sm text-muted-foreground">
                    This run is paused for human review of conflicts, findings, and
                    register changes.
                  </p>
                </div>
                <Button asChild size="lg">
                  <Link to={`/runs/${id}/review`}>Review now</Link>
                </Button>
              </CardContent>
            </Card>
          ) : null}

          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-foreground">Stages</h2>
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Circle
                  className={cn(
                    'size-2 fill-current',
                    sseStatus === 'open' ? 'text-emerald-600' : 'text-muted-foreground',
                  )}
                />
                {sseStatus === 'open' ? 'Live' : sseStatus}
              </span>
            </div>
            <div className="flex flex-col gap-3">
              {STAGE_ORDER.map((name) => {
                const stage = stages[name]
                const displayStage: StageState =
                  runFailed && stage.status === 'running'
                    ? { ...stage, status: 'failed' }
                    : stage
                return <StageCard key={name} stage={displayStage} />
              })}
            </div>
          </div>
        </>
      ) : null}
    </div>
  )
}
