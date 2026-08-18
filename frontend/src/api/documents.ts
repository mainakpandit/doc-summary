import { fetchJson } from '@/api/client'

export type DocumentText = {
  document_id: string
  filename: string
  text: string
}

export function getDocumentText(corpusId: string, documentId: string): Promise<DocumentText> {
  return fetchJson(`/api/corpora/${corpusId}/documents/${documentId}/text`)
}
