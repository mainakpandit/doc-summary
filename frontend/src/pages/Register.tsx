import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Download, Inbox, Info } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { SourceViewer } from '@/components/SourceViewer'
import { listCorpora } from '@/api/corpora'
import { getRegister, type FieldClaim, type RegisterEntry } from '@/api/register'
import type { Citation } from '@/api/reviews'

type SortKey = 'feature' | 'owner' | 'target_release' | 'status' | 'open_risks' | 'sources'
type SortDirection = 'asc' | 'desc'
type SortState = { key: SortKey; direction: SortDirection }

const PREFERRED_FIELD_ORDER = ['name', 'owner', 'target_release', 'status', 'open_risks']

function nonEmpty(value: unknown): boolean {
  if (value == null) return false
  if (typeof value === 'string') return value.trim().length > 0
  if (Array.isArray(value)) return value.length > 0
  return true
}

function dedupeClaims(claimLists: FieldClaim[][]): FieldClaim[] {
  const seen = new Set<string>()
  const claims: FieldClaim[] = []
  for (const list of claimLists) {
    for (const claim of list) {
      if (!seen.has(claim.claim_id)) {
        seen.add(claim.claim_id)
        claims.push(claim)
      }
    }
  }
  return claims
}

function allClaims(entry: RegisterEntry): FieldClaim[] {
  return dedupeClaims(Object.values(entry.field_claims))
}

function allSources(entry: RegisterEntry): Citation[] {
  const seen = new Set<string>()
  const sources: Citation[] = []
  for (const claim of allClaims(entry)) {
    for (const source of claim.sources) {
      const key = `${source.chunk_id}:${source.char_start}:${source.char_end}`
      if (!seen.has(key)) {
        seen.add(key)
        sources.push(source)
      }
    }
  }
  return sources
}

function sortValue(entry: RegisterEntry, key: SortKey): string | number {
  switch (key) {
    case 'feature':
      return (entry.fields.name || entry.feature_key).toLowerCase()
    case 'owner':
      return (entry.fields.owner ?? '').toLowerCase()
    case 'target_release':
      return (entry.fields.target_release ?? '').toLowerCase()
    case 'status':
      return (entry.fields.status ?? '').toLowerCase()
    case 'open_risks':
      return (entry.fields.open_risks ?? []).length
    case 'sources':
      return allSources(entry).length
  }
}

function sortEntries(entries: RegisterEntry[], sort: SortState): RegisterEntry[] {
  const sorted = [...entries].sort((a, b) => {
    const va = sortValue(a, sort.key)
    const vb = sortValue(b, sort.key)
    const cmp =
      typeof va === 'number' && typeof vb === 'number' ? va - vb : String(va).localeCompare(String(vb))
    return sort.direction === 'asc' ? cmp : -cmp
  })
  return sorted
}

function flattenFieldValue(value: unknown): string {
  if (value == null) return ''
  if (Array.isArray(value)) return value.map(String).join('; ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function csvEscape(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}

function fieldColumnOrder(entries: RegisterEntry[]): string[] {
  const allKeys = new Set(entries.flatMap((entry) => Object.keys(entry.fields)))
  const preferred = PREFERRED_FIELD_ORDER.filter((key) => allKeys.has(key))
  const extra = [...allKeys].filter((key) => !PREFERRED_FIELD_ORDER.includes(key)).sort()
  return [...preferred, ...extra]
}

function downloadCsv(entries: RegisterEntry[]): void {
  const fieldKeys = fieldColumnOrder(entries)
  const header = ['feature_key', ...fieldKeys]
  const rows = entries.map((entry) =>
    header.map((key) => (key === 'feature_key' ? entry.feature_key : flattenFieldValue(entry.fields[key]))),
  )
  const csv = [header, ...rows].map((row) => row.map(csvEscape).join(',')).join('\r\n')

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `feature-register-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function SourceLink({ corpusId, citation }: { corpusId: string; citation: Citation }) {
  return (
    <Sheet>
      <SheetTrigger asChild>
        <button
          type="button"
          className="rounded border px-1.5 py-0.5 text-xs whitespace-nowrap text-muted-foreground transition-colors hover:border-foreground hover:text-foreground"
        >
          {citation.document_filename}
          {citation.page != null ? ` p.${citation.page}` : ''}
        </button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{citation.document_filename || 'Source'}</SheetTitle>
          <SheetDescription>
            {citation.page != null ? `Page ${citation.page} — ` : ''}cited quote highlighted below.
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
  )
}

function ClaimsPopover({ corpusId, claims }: { corpusId: string; claims: FieldClaim[] }) {
  if (claims.length === 0) return null

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex shrink-0 items-center justify-center text-muted-foreground transition-colors hover:text-foreground"
          aria-label="Show backing claims"
        >
          <Info className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="max-h-96 overflow-y-auto">
        <div className="flex flex-col gap-3">
          {claims.map((claim) => (
            <div key={claim.claim_id} className="flex flex-col gap-1.5 border-b pb-3 last:border-0 last:pb-0">
              <div className="flex items-start justify-between gap-2">
                <p className="text-sm text-foreground">
                  <span className="text-muted-foreground">{claim.predicate}:</span> {claim.object}
                </p>
                <Badge variant="outline" className="shrink-0">
                  {Math.round(claim.confidence * 100)}%
                </Badge>
              </div>
              {claim.sources.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {claim.sources.map((source, i) => (
                    <SourceLink key={`${source.chunk_id}-${i}`} corpusId={corpusId} citation={source} />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}

function RegisterCell({
  corpusId,
  claims,
  empty,
  children,
}: {
  corpusId: string
  claims: FieldClaim[]
  empty: boolean
  children: ReactNode
}) {
  if (empty) {
    return <span className="text-muted-foreground">—</span>
  }
  return (
    <div className="flex items-center gap-1.5">
      <span className="min-w-0">{children}</span>
      <ClaimsPopover corpusId={corpusId} claims={claims} />
    </div>
  )
}

function SortableHead({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string
  sortKey: SortKey
  sort: SortState
  onSort: (key: SortKey) => void
}) {
  const active = sort.key === sortKey
  const Icon = active ? (sort.direction === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown

  return (
    <TableHead>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className="inline-flex items-center gap-1 font-medium text-foreground transition-colors hover:text-foreground/80"
      >
        {label}
        <Icon className={active ? 'size-3.5' : 'size-3.5 text-muted-foreground/50'} />
      </button>
    </TableHead>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed py-16 text-center">
      <Inbox className="size-8 text-muted-foreground" />
      <p className="text-sm font-medium text-foreground">No register entries yet</p>
      <p className="max-w-sm text-sm text-muted-foreground">
        Run this corpus and approve its register changes at the review gate to populate the Feature
        Register.
      </p>
    </div>
  )
}

export function Register() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [sort, setSort] = useState<SortState>({ key: 'feature', direction: 'asc' })

  const corporaQuery = useQuery({
    queryKey: ['corpora'],
    queryFn: listCorpora,
    staleTime: 60_000,
  })

  const corpusId = searchParams.get('corpus') ?? corporaQuery.data?.[0]?.id ?? null

  const registerQuery = useQuery({
    queryKey: ['register', corpusId],
    queryFn: () => getRegister(corpusId!),
    enabled: Boolean(corpusId),
  })

  function handleSort(key: SortKey) {
    setSort((prev) =>
      prev.key === key ? { key, direction: prev.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' },
    )
  }

  function handleCorpusChange(id: string) {
    setSearchParams(id ? { corpus: id } : {})
  }

  const entries = registerQuery.data ?? []
  const sorted = sortEntries(entries, sort)

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-foreground">Feature Register</h1>

        <div className="flex items-center gap-2">
          <label htmlFor="register-corpus" className="text-sm text-muted-foreground">
            Corpus
          </label>
          <select
            id="register-corpus"
            value={corpusId ?? ''}
            onChange={(e) => handleCorpusChange(e.target.value)}
            disabled={(corporaQuery.data ?? []).length === 0}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm text-foreground shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50"
          >
            {(corporaQuery.data ?? []).map((corpus) => (
              <option key={corpus.id} value={corpus.id}>
                {corpus.name}
              </option>
            ))}
          </select>
          <Button
            variant="outline"
            size="sm"
            onClick={() => downloadCsv(entries)}
            disabled={entries.length === 0}
          >
            <Download />
            Download CSV
          </Button>
        </div>
      </div>

      {!corpusId ? (
        corporaQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading corpora…</p>
        ) : corporaQuery.isError ? (
          <p className="text-sm text-destructive">{(corporaQuery.error as Error).message}</p>
        ) : (
          <p className="text-sm text-muted-foreground">No corpora yet.</p>
        )
      ) : registerQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading register…</p>
      ) : registerQuery.isError ? (
        <p className="text-sm text-destructive">{(registerQuery.error as Error).message}</p>
      ) : entries.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <SortableHead label="Feature" sortKey="feature" sort={sort} onSort={handleSort} />
                <SortableHead label="Owner" sortKey="owner" sort={sort} onSort={handleSort} />
                <SortableHead label="Target Release" sortKey="target_release" sort={sort} onSort={handleSort} />
                <SortableHead label="Status" sortKey="status" sort={sort} onSort={handleSort} />
                <SortableHead label="Open Risks" sortKey="open_risks" sort={sort} onSort={handleSort} />
                <SortableHead label="Sources" sortKey="sources" sort={sort} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((entry) => {
                const sources = allSources(entry)
                return (
                  <TableRow key={entry.id}>
                    <TableCell className="font-medium text-foreground">
                      <RegisterCell
                        corpusId={corpusId}
                        claims={entry.field_claims.name ?? []}
                        empty={false}
                      >
                        {entry.fields.name || entry.feature_key}
                      </RegisterCell>
                    </TableCell>
                    <TableCell>
                      <RegisterCell
                        corpusId={corpusId}
                        claims={entry.field_claims.owner ?? []}
                        empty={!nonEmpty(entry.fields.owner)}
                      >
                        {entry.fields.owner}
                      </RegisterCell>
                    </TableCell>
                    <TableCell>
                      <RegisterCell
                        corpusId={corpusId}
                        claims={entry.field_claims.target_release ?? []}
                        empty={!nonEmpty(entry.fields.target_release)}
                      >
                        {entry.fields.target_release}
                      </RegisterCell>
                    </TableCell>
                    <TableCell>
                      <RegisterCell
                        corpusId={corpusId}
                        claims={entry.field_claims.status ?? []}
                        empty={!nonEmpty(entry.fields.status)}
                      >
                        {entry.fields.status}
                      </RegisterCell>
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <RegisterCell
                        corpusId={corpusId}
                        claims={entry.field_claims.open_risks ?? []}
                        empty={!nonEmpty(entry.fields.open_risks)}
                      >
                        <div className="flex flex-wrap gap-1">
                          {(entry.fields.open_risks ?? []).map((risk, i) => (
                            <Badge key={`${entry.id}-risk-${i}`} variant="destructive">
                              {risk}
                            </Badge>
                          ))}
                        </div>
                      </RegisterCell>
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <RegisterCell corpusId={corpusId} claims={allClaims(entry)} empty={sources.length === 0}>
                        <div className="flex flex-wrap gap-1">
                          {sources.map((source, i) => (
                            <SourceLink key={`${source.chunk_id}-${i}`} corpusId={corpusId} citation={source} />
                          ))}
                        </div>
                      </RegisterCell>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
