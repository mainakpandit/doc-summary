/** Thin wrapper around `EventSource` that reconnects on disconnect.
 *
 * Native `EventSource` only dispatches named events (the `event:` field the
 * server sends) to listeners registered for that exact name via
 * `addEventListener`, so callers must know the set of event types they
 * expect up front -- `/runs/{id}/events` emits one per graph node
 * (`{node}_start` / `{node}_end`) plus `run_completed` / `run_failed`.
 */

export type SseEvent = {
  type: string
  data: unknown
}

export type SseStatus = 'connecting' | 'open' | 'closed'

export type SseConnection = {
  close: () => void
}

const RECONNECT_DELAY_MS = 3000

export function connectSse(
  url: string,
  eventTypes: readonly string[],
  onEvent: (event: SseEvent) => void,
  onStatusChange?: (status: SseStatus) => void,
): SseConnection {
  let source: EventSource | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let closedByCaller = false

  function connect() {
    onStatusChange?.('connecting')
    source = new EventSource(url)

    source.onopen = () => onStatusChange?.('open')

    for (const type of eventTypes) {
      source.addEventListener(type, (evt) => {
        const raw = (evt as MessageEvent<string>).data
        let data: unknown = null
        if (raw) {
          try {
            data = JSON.parse(raw)
          } catch {
            data = raw
          }
        }
        onEvent({ type, data })
      })
    }

    source.onerror = () => {
      source?.close()
      onStatusChange?.('closed')
      if (!closedByCaller) {
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS)
      }
    }
  }

  connect()

  return {
    close: () => {
      closedByCaller = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      source?.close()
    },
  }
}
