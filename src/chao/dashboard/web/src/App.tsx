import { useEffect, useRef, useState } from 'react'

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
// Mirrors free_speech.py's MIN_INTERVAL_S -- the server clamps to this
// floor regardless, but showing the real floor here avoids a control that
// silently does something other than what it displays.
const MIN_FREE_SPEECH_INTERVAL_S = 15
const DEFAULT_FREE_SPEECH_INTERVAL_S = 90
// config/emotes.yaml's real pool names -- offered as autocomplete only,
// not validated client-side. The server silently no-ops an unknown pool
// (director.py's own `_fire_pool` does the same for a bad tag), so a typo
// here fails the same safe way a bad tag would.
const EMOTE_POOLS = [
  'happy_eyes',
  'sad_eyes',
  'angry_eyes',
  'confused_eyes',
  'question_bub',
  'surprise_bub',
  'confused_bub',
  'heart_bub',
]

// Force-mood presets: raw valence/arousal number inputs turned out to be
// unintuitive for a quick check, so these are fixed target points instead
// of free-form values. happy/sad/angry/confused reuse mood.py's own
// _TAG_DELTAS numbers (there they're deltas added to whatever the point
// currently is; here they're absolute targets -- close enough in spirit
// since they're the same "shape" per emotion). reset mirrors
// config/emotes.yaml's mood.baseline_valence/baseline_arousal.
const MOOD_PRESETS: Record<string, [number, number]> = {
  happy: [0.7, 0.6],
  sad: [-0.7, -0.4],
  angry: [-0.6, 0.8],
  confused: [-0.2, 0.4],
  reset: [0.2, 0.3],
}

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
  // Session 11: the command channel back to the server (dashboard/server.py
  // set_backend/set_free_speech). Optimistic local state only -- there's no
  // state echo from the server, so this reflects "what was last sent," not
  // a confirmed round trip. brain.request's own "backend" field in the feed
  // is the actual confirmation of what served the last turn.
  const [backend, setBackend] = useState<'auto' | 'cloud' | 'local'>('auto')
  const [freeSpeechEnabled, setFreeSpeechEnabled] = useState(false)
  const [freeSpeechInterval, setFreeSpeechInterval] = useState(DEFAULT_FREE_SPEECH_INTERVAL_S)
  // Session 11 part 6: the rest of the override panel (design doc §11.2
  // item 8). Each field is local, uncontrolled-in-spirit form state --
  // cleared after sending, since these are one-shot actions, not settings
  // that persist like backend/free-speech above.
  const [emotePool, setEmotePool] = useState('')
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws
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

  function sendCommand(command: Record<string, unknown>) {
    wsRef.current?.send(JSON.stringify(command))
  }

  function handleBackendChange(value: 'auto' | 'cloud' | 'local') {
    setBackend(value)
    sendCommand({ type: 'set_backend', backend: value })
  }

  function handleFreeSpeechChange(enabled: boolean, intervalS: number) {
    setFreeSpeechEnabled(enabled)
    setFreeSpeechInterval(intervalS)
    sendCommand({ type: 'set_free_speech', enabled, interval_s: intervalS })
  }

  function handleFireEmote() {
    if (!emotePool.trim()) return
    sendCommand({ type: 'fire_emote', pool: emotePool.trim() })
  }

  function handleForceMoodPreset(preset: string) {
    const [valence, arousal] = MOOD_PRESETS[preset]
    sendCommand({ type: 'force_mood', valence, arousal })
  }

  function handleKill() {
    sendCommand({ type: 'kill' })
  }

  function handleRevive() {
    sendCommand({ type: 'revive' })
  }

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <h1>chao dashboard</h1>
        <span className={connected ? 'status status-up' : 'status status-down'}>
          {connected ? 'connected' : 'disconnected'}
        </span>
      </header>

      <div className="controls">
        <label className="control">
          Backend
          <select
            value={backend}
            onChange={(e) => handleBackendChange(e.target.value as 'auto' | 'cloud' | 'local')}
          >
            <option value="auto">auto</option>
            <option value="cloud">cloud</option>
            <option value="local">local</option>
          </select>
        </label>
        <label className="control">
          <input
            type="checkbox"
            checked={freeSpeechEnabled}
            onChange={(e) => handleFreeSpeechChange(e.target.checked, freeSpeechInterval)}
          />
          Free speech, every
          <input
            type="number"
            className="interval"
            min={MIN_FREE_SPEECH_INTERVAL_S}
            value={freeSpeechInterval}
            onChange={(e) =>
              handleFreeSpeechChange(freeSpeechEnabled, Number(e.target.value))
            }
          />
          s
        </label>
      </div>

      <div className="overrides">
        <div className="override">
          <input
            list="emote-pools"
            placeholder="pool, e.g. happy_eyes"
            value={emotePool}
            onChange={(e) => setEmotePool(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleFireEmote()}
          />
          <datalist id="emote-pools">
            {EMOTE_POOLS.map((pool) => (
              <option value={pool} key={pool} />
            ))}
          </datalist>
          <button onClick={handleFireEmote}>Fire emote</button>
        </div>

        <div className="override">
          Mood
          {Object.keys(MOOD_PRESETS).map((preset) => (
            <button key={preset} onClick={() => handleForceMoodPreset(preset)}>
              {preset}
            </button>
          ))}
        </div>

        <div className="override">
          <button className="danger" onClick={handleKill}>
            Kill
          </button>
          <button onClick={handleRevive}>Revive</button>
        </div>
      </div>

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
