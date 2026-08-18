import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getDocumentText } from '@/api/documents'
import { ScrollArea } from '@/components/ui/scroll-area'

export type SourceViewerProps = {
  corpusId: string
  documentId: string
  charStart: number
  charEnd: number
}

export function SourceViewer({ corpusId, documentId, charStart, charEnd }: SourceViewerProps) {
  const highlightRef = useRef<HTMLSpanElement>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['document-text', corpusId, documentId],
    queryFn: () => getDocumentText(corpusId, documentId),
  })

  useEffect(() => {
    if (data) {
      highlightRef.current?.scrollIntoView({ block: 'center' })
    }
  }, [data])

  if (isLoading) {
    return <p className="p-4 text-sm text-muted-foreground">Loading source…</p>
  }
  if (isError) {
    return <p className="p-4 text-sm text-destructive">{(error as Error).message}</p>
  }
  if (!data) return null

  const text = data.text
  const start = Math.max(0, Math.min(charStart, text.length))
  const end = Math.max(start, Math.min(charEnd, text.length))

  return (
    <ScrollArea className="min-h-0 flex-1">
      <pre className="whitespace-pre-wrap wrap-break-word p-4 font-mono text-sm text-foreground">
        {text.slice(0, start)}
        <span ref={highlightRef} className="bg-yellow-300/70">
          {text.slice(start, end)}
        </span>
        {text.slice(end)}
      </pre>
    </ScrollArea>
  )
}
