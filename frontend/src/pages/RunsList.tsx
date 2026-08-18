import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { listCorpora } from '@/api/corpora'
import {
  getRunCost,
  listRuns,
  TERMINAL_RUN_STATUSES,
  type RunRead,
  type RunStatus,
} from '@/api/runs'
import { cn } from '@/lib/utils'

const POLL_MS = 3000

const STATUS_CLASSES: Record<RunStatus, string> = {
  pending: 'bg-secondary text-secondary-foreground',
  running: 'bg-primary text-primary-foreground',
  committing: 'bg-primary text-primary-foreground',
  awaiting_review: 'bg-amber-500 text-white hover:bg-amber-500',
  done: 'bg-emerald-600 text-white hover:bg-emerald-600',
  failed: 'bg-destructive text-white',
  cancelled: 'bg-muted text-muted-foreground',
}

function shortId(id: string): string {
  return id.slice(0, 8)
}

function formatStartedAt(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function formatUsd(value: number): string {
  return `$${value.toFixed(4)}`
}

function RunCostCell({ run }: { run: RunRead }) {
  const isTerminal = TERMINAL_RUN_STATUSES.includes(run.status)
  const { data, isLoading } = useQuery({
    queryKey: ['run-cost', run.id],
    queryFn: () => getRunCost(run.id),
    refetchInterval: isTerminal ? false : POLL_MS,
  })

  if (isLoading || !data) {
    return <span className="text-muted-foreground">—</span>
  }
  return <span>{formatUsd(data.total_usd_cost)}</span>
}

export function RunsList() {
  const navigate = useNavigate()

  const runsQuery = useQuery({
    queryKey: ['runs'],
    queryFn: listRuns,
    refetchInterval: POLL_MS,
  })

  const corporaQuery = useQuery({
    queryKey: ['corpora'],
    queryFn: listCorpora,
    staleTime: 60_000,
  })

  const corpusNameById = new Map((corporaQuery.data ?? []).map((c) => [c.id, c.name]))

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="mb-4 text-xl font-bold text-foreground">Runs</h1>

      {runsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading runs…</p>
      ) : runsQuery.isError ? (
        <p className="text-sm text-destructive">
          {(runsQuery.error as Error).message}
        </p>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Corpus</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Cost so far</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(runsQuery.data ?? []).length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={6}
                    className="text-center text-muted-foreground"
                  >
                    No runs yet.
                  </TableCell>
                </TableRow>
              ) : (
                (runsQuery.data ?? []).map((run) => (
                  <TableRow
                    key={run.id}
                    className="cursor-pointer"
                    onClick={() => navigate(`/runs/${run.id}`)}
                  >
                    <TableCell className="font-mono">
                      {shortId(run.id)}
                    </TableCell>
                    <TableCell>
                      {corpusNameById.get(run.corpus_id) ?? shortId(run.corpus_id)}
                    </TableCell>
                    <TableCell>{run.kind}</TableCell>
                    <TableCell>
                      <Badge className={cn(STATUS_CLASSES[run.status])}>
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell>{formatStartedAt(run.started_at)}</TableCell>
                    <TableCell>
                      <RunCostCell run={run} />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
