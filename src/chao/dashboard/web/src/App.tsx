import { useEffect, useState } from 'react'

// See design doc §11.2 — phase 1 ships only the event feed panel (item 3),
// the direct replacement for __main__.py's old console printing. The other
// eight panels render data nothing in the backend produces yet.
type ChaoEvent = {
  kind: string
  payload: Record<string, unknown>
  id: string
  ts: number
  turn_id: string | null
}

const WS_URL = 'ws://127.0.0.1:8765/ws/events'
const MAX_ENTRIES = 200

function summarize(event: ChaoEvent): string {
  const p = event.payload
  switch (event.kind) {
    case 'brain.complete':
      return `"${p.full_text}" (${Math.round(Number(p.latency_ms))}ms)`
    case 'director.emote':
      return `${p.pool} -> ${p.hotkey_id} (${p.reason})`
    case 'director.tag':
      return `${p.tag} @ sentence ${p.sentence_index}`
    case 'decision.dropped':
      return `reason: ${p.reason}`
    case 'decision.selected':
      return `priority: ${p.priority}`
    case 'error':
      return `${p.component}: ${p.message}`
    default:
      return JSON.stringify(p)
  }
}

export default function App() {
  const [connected, setConnected] = useState(false)
  const [entries, setEntries] = useState<ChaoEvent[]>([])
  // Keyed by turn_id: the in-progress reply text, assembled from
  // brain.token chunks and folded into a normal entry on brain.complete.
  // A single in-flight turn (bus.py's arbitration) means this is normally
  // at most one entry, but keying by turn_id avoids assuming that.
  const [streaming, setStreaming] = useState<Record<string, string>>({})
  // vts.param telemetry (idle drift + speech motion) fires several times a
  // second, continuously -- even sampled server-side (outputs/vts.py's
  // publish_every_n_frames), it drowned the transcript out of the capped
  // MAX_ENTRIES feed within seconds. Kept out of `entries` entirely (never
  // spends a feed slot) and shown as a live current-value readout instead
  // -- a minimal version of design doc §11.2 item 5 (parameter sparklines),
  // not a scrolling log of its own.
  const [params, setParams] = useState<Record<string, number>>({})

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onmessage = (msg) => {
      const event = JSON.parse(msg.data as string) as ChaoEvent

      if (event.kind === 'vts.param') {
        const name = event.payload.name as string
        const value = event.payload.value as number
        setParams((prev) => ({ ...prev, [name]: value }))
        return
      }

      if (event.kind === 'brain.token') {
        const key = event.turn_id ?? 'unknown'
        const text = event.payload.text as string
        setStreaming((prev) => ({ ...prev, [key]: (prev[key] ?? '') + text }))
        return
      }

      if (event.kind === 'brain.complete' && event.turn_id) {
        const turnId = event.turn_id
        setStreaming((prev) => {
          const next = { ...prev }
          delete next[turnId]
          return next
        })
      }

      setEntries((prev) => [event, ...prev].slice(0, MAX_ENTRIES))
    }
    return () => ws.close()
  }, [])

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <h1>chao dashboard</h1>
        <span className={connected ? 'status status-up' : 'status status-down'}>
          {connected ? 'connected' : 'disconnected'}
        </span>
      </header>

      {Object.keys(params).length > 0 && (
        <div className="params">
          {Object.entries(params)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([name, value]) => (
              <span className="param" key={name}>
                {name}: {value.toFixed(1)}
              </span>
            ))}
        </div>
      )}

      {Object.values(streaming).map((text, i) => (
        <div className="streaming" key={i}>
          {text}
          <span className="cursor" />
        </div>
      ))}

      <ul className="feed">
        {entries.map((event) => (
          <li key={event.id} className={`entry entry-${event.kind.split('.')[0]}`}>
            <span className="ts">{event.ts.toFixed(2)}</span>
            <span className="kind">{event.kind}</span>
            <span className="detail">{summarize(event)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
