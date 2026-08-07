# chao dashboard

Vite + React frontend for the dashboard (design doc §11). Connects to
`ws://127.0.0.1:8765/ws/events`, which `uv run python -m chao` serves
in-process. Currently ships only the event feed panel (§11.2 item 3) —
the other panels need backend features (retrieval, mood, parameter
injection) that don't exist yet.

```bash
npm install
npm run dev
```
