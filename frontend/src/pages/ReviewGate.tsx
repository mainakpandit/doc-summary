import { useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { SourceViewer } from '@/components/SourceViewer'
import { getRun } from '@/api/runs'
import {
  getReview,
  resumeRun,
  submitReview,
  type Citation,
  type ReviewDecision,
  type ReviewItemType,
} from '@/api/reviews'
import { useReviewerStore } from '@/store/reviewer'
import { toast } from '@/store/toast'

type DecisionState = {
  decision: ReviewDecision | null
  note: string
}

function itemKey(itemType: ReviewItemType, id: string): string {
  return `${itemType}:${id}`
}

function splitItemKey(key: string): [ReviewItemType, string] {
  const idx = key.indexOf(':')
  return [key.slice(0, idx) as ReviewItemType, key.slice(idx + 1)]
}

function SourceButtons({ corpusId, citations }: { corpusId: string; citations: Citation[] }) {
  if (citations.length === 0) return null

  return (
    <div className="flex flex-wrap gap-2">
      {citations.map((citation, i) => (
        <Sheet key={`${citation.chunk_id}-${i}`}>
          <SheetTrigger asChild>
            <Button variant="outline" size="sm">
              View source{citations.length > 1 ? `: ${citation.document_filename}` : ''}
            </Button>
          </SheetTrigger>
          <SheetContent side="right" className="w-full sm:max-w-xl">
            <SheetHeader>
              <SheetTitle>{citation.document_filename || 'Source'}</SheetTitle>
              <SheetDescription>
                {citation.page != null ? `Page ${citation.page} — ` : ''}cited quote highlighted
                below.
              </SheetDescription>
            </SheetHeader>
            <SourceViewer
              corpusId={corpusId}
              documentId={citation.document_id}
              charStart={citation.char_start}
              charEnd={citation.char_end}
            />
          </SheetContent>
        </Sheet>
      ))}
    </div>
  )
}

function ReviewItemCard({
  title,
  description,
  badge,
  corpusId,
  citations,
  decision,
  note,
  onDecide,
  onNote,
}: {
  title: string
  description?: string
  badge?: ReactNode
  corpusId: string
  citations: Citation[]
  decision: ReviewDecision | null
  note: string
  onDecide: (decision: ReviewDecision | null) => void
  onNote: (note: string) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <span>{title}</span>
          {badge}
        </CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <SourceButtons corpusId={corpusId} citations={citations} />

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={decision === 'approve' ? 'default' : 'outline'}
            onClick={() => onDecide(decision === 'approve' ? null : 'approve')}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant={decision === 'reject' ? 'destructive' : 'outline'}
            onClick={() => onDecide(decision === 'reject' ? null : 'reject')}
          >
            Reject
          </Button>
          {decision ? (
            <Badge variant={decision === 'approve' ? 'default' : 'destructive'}>{decision}</Badge>
          ) : (
            <span className="text-xs text-muted-foreground">No decision yet</span>
          )}
        </div>

        <Input
          placeholder="Add a note (optional)"
          value={note}
          onChange={(e) => onNote(e.target.value)}
          className="max-w-md"
        />
      </CardContent>
    </Card>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold text-foreground">{title}</h2>
      <div className="flex flex-col gap-3">{children}</div>
    </div>
  )
}

function EmptyState({ label }: { label: string }) {
  return <p className="text-sm text-muted-foreground">{label}</p>
}

export function ReviewGate() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const reviewer = useReviewerStore((s) => s.reviewer)
  const [decisions, setDecisions] = useState<Record<string, DecisionState>>({})

  const runQuery = useQuery({
    queryKey: ['run', id],
    queryFn: () => getRun(id!),
    enabled: Boolean(id),
  })

  const reviewQuery = useQuery({
    queryKey: ['review', id],
    queryFn: () => getReview(id!),
    enabled: Boolean(id),
  })

  function getDecision(key: string): DecisionState {
    return decisions[key] ?? { decision: null, note: '' }
  }

  function setDecision(key: string, decision: ReviewDecision | null) {
    setDecisions((prev) => ({ ...prev, [key]: { ...getDecision(key), decision } }))
  }

  function setNote(key: string, note: string) {
    setDecisions((prev) => ({ ...prev, [key]: { ...getDecision(key), note } }))
  }

  const submitMutation = useMutation({
    mutationFn: async () => {
      const items = Object.entries(decisions)
        .filter(([, d]) => d.decision)
        .map(([key, d]) => {
          const [item_type, itemId] = splitItemKey(key)
          return {
            id: itemId,
            item_type,
            decision: d.decision as ReviewDecision,
            note: d.note.trim() || undefined,
          }
        })
      if (items.length === 0) {
        throw new Error('Decide on at least one item before submitting.')
      }
      await submitReview(id!, items, reviewer)
      return resumeRun(id!)
    },
    onSuccess: () => {
      toast({ title: 'Decisions submitted', description: 'The run has resumed.' })
      navigate(`/runs/${id}`)
    },
    onError: (err) => {
      toast({
        title: 'Submit failed',
        description: (err as Error).message,
        variant: 'destructive',
      })
    },
  })

  if (!id) return null

  const corpusId = runQuery.data?.corpus_id
  const decidedCount = Object.values(decisions).filter((d) => d.decision).length
  const totalCount = reviewQuery.data
    ? reviewQuery.data.conflicts.length +
      reviewQuery.data.findings.length +
      reviewQuery.data.register_changes.length
    : 0

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 pb-24">
      <div>
        <p className="text-sm text-muted-foreground">Review gate for run</p>
        <h1 className="font-mono text-xl font-bold text-foreground">{id}</h1>
      </div>

      {runQuery.isLoading || reviewQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading review items…</p>
      ) : runQuery.isError ? (
        <p className="text-sm text-destructive">{(runQuery.error as Error).message}</p>
      ) : reviewQuery.isError ? (
        <p className="text-sm text-destructive">{(reviewQuery.error as Error).message}</p>
      ) : reviewQuery.data && corpusId ? (
        <>
          <Section title="Conflicts">
            {reviewQuery.data.conflicts.length === 0 ? (
              <EmptyState label="No conflicts pending review." />
            ) : (
              reviewQuery.data.conflicts.map((conflict) => {
                const key = itemKey('conflict', conflict.id)
                const state = getDecision(key)
                return (
                  <ReviewItemCard
                    key={key}
                    title={`${conflict.subject} — ${conflict.predicate}`}
                    description={`"${conflict.claim_a.object}" vs. "${conflict.claim_b.object}"`}
                    corpusId={corpusId}
                    citations={[...conflict.claim_a.sources, ...conflict.claim_b.sources]}
                    decision={state.decision}
                    note={state.note}
                    onDecide={(d) => setDecision(key, d)}
                    onNote={(n) => setNote(key, n)}
                  />
                )
              })
            )}
          </Section>

          <Section title="Findings">
            {reviewQuery.data.findings.length === 0 ? (
              <EmptyState label="No findings pending review." />
            ) : (
              reviewQuery.data.findings.map((finding) => {
                const key = itemKey('finding', finding.id)
                const state = getDecision(key)
                return (
                  <ReviewItemCard
                    key={key}
                    title={finding.message}
                    description={finding.subject ?? undefined}
                    badge={<Badge variant="outline">{finding.severity}</Badge>}
                    corpusId={corpusId}
                    citations={finding.sources}
                    decision={state.decision}
                    note={state.note}
                    onDecide={(d) => setDecision(key, d)}
                    onNote={(n) => setNote(key, n)}
                  />
                )
              })
            )}
          </Section>

          <Section title="Register Changes">
            {reviewQuery.data.register_changes.length === 0 ? (
              <EmptyState label="No register changes pending review." />
            ) : (
              reviewQuery.data.register_changes.map((change) => {
                const key = itemKey('register_change', change.id)
                const state = getDecision(key)
                const title =
                  change.change_kind === 'addition'
                    ? `Add feature "${change.feature_key}"`
                    : `${change.feature_key}.${change.field_name}`
                const description =
                  change.change_kind === 'field_change'
                    ? `${JSON.stringify(change.old_value)} → ${JSON.stringify(change.new_value)}`
                    : undefined
                return (
                  <ReviewItemCard
                    key={key}
                    title={title}
                    description={description}
                    badge={<Badge variant="outline">{change.change_kind}</Badge>}
                    corpusId={corpusId}
                    citations={change.sources}
                    decision={state.decision}
                    note={state.note}
                    onDecide={(d) => setDecision(key, d)}
                    onNote={(n) => setNote(key, n)}
                  />
                )
              })
            )}
          </Section>

          <div className="fixed inset-x-0 bottom-0 flex items-center justify-between gap-4 border-t bg-background/95 px-6 py-4 backdrop-blur">
            <p className="text-sm text-muted-foreground">
              {decidedCount} of {totalCount} decided
              {!reviewer.trim() ? ' — set your reviewer name in the top nav' : ''}
            </p>
            <Button
              size="lg"
              onClick={() => submitMutation.mutate()}
              disabled={submitMutation.isPending || !reviewer.trim()}
            >
              {submitMutation.isPending ? 'Submitting…' : 'Submit decisions'}
            </Button>
          </div>
        </>
      ) : null}
    </div>
  )
}
