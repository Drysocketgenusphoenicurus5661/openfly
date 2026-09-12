# OpenFly web frontend

The browser UI for OpenFly: what the fly saw, what it spiked, what the
readout predicted, what the guard did, the straddle against its stop and
target, and P&L against the controls. Built with Vite, React 19,
TypeScript, Tailwind v4, shadcn (new-york, neutral), openalgo-charts,
TanStack Query, zustand and react-router.

## Commands

```
pnpm install          install dependencies (Node 20.20+, pnpm 10)
pnpm dev              Vite dev server on http://localhost:5173, /api proxied to 127.0.0.1:8000
pnpm build            type check and build to frontend/dist (served by the FastAPI backend)
pnpm preview          serve the production build locally
pnpm lint             biome lint
pnpm format           biome format, in place
pnpm check            biome check with fixes
pnpm test             vitest in watch mode
pnpm test:run         vitest once
```

`uv run app.py` at the repository root builds this frontend when
`frontend/dist` is missing and serves it with the API.

## Mock mode

The UI is fully demoable without the backend. At startup the app probes
`GET /api/status`; if the request fails, times out, or does not return
JSON, every API call is answered by `src/mock/server.ts` and the websocket
is replaced by `src/mock/events.ts`. Adding `?mock=1` to the URL forces
mock mode even when the backend is up. A "mock data" badge in the header
shows which mode is active.

The mock day (`src/mock/day.ts`) is a scripted 2026-09-11 with 375 one
minute bars: NIFTY near 23400, INDIAVIX 12.3, a first straddle sold at
10:20 and stopped on the combined premium at 11:30 after a volatility
expansion, a re-entry at 11:45 at a new strike that locks at 12:30, has
its call leg stopped at the broker at 13:05 and its put leg reach target
at 14:40, and vetoes at 09:40 and 14:50. The mock worker "runs" through
that day two seconds per minute, so the Dashboard, Brain, Orders and the
event stream move. Replays, experiments, settings and the data pipeline
are all served with plausible generated data and progress events.

## Layout

```
src/api          typed client (client.ts), types mirroring docs/api-spec.md,
                 type guards, TanStack Query hooks, the /api/events hook
                 with reconnect and a 500 event ring buffer, mock mode switch
src/mock         offline data: the scripted trading day, experiments,
                 stimulus images, the in-memory router and event ticker
src/components   charts (openalgo-charts wrappers), decision panel, brain
                 widgets, replay widgets, layout (sidebar, header, session
                 clock, kill switch), shadcn ui
src/pages        Dashboard, Brain, Replay, Experiments, Orders, Settings, Setup
src/stores       zustand stores (replay player)
src/hooks        latest decision, session clock, replay clock, virtual rows
src/lib          formatting, IST time helpers, action colours, chart data
```

## Conventions

Plain text everywhere: no emoji, no icons in text, no em or en dashes.
lucide icons appear only in navigation and on buttons. Numbers use
tabular figures. Actions are colour coded the same way on badges, the
timeline and chart markers: ENTER and REENTRY green, EXIT and TARGET blue,
STOP and STOP_LEG red, SQUARE_OFF orange, LOCK violet, VETO grey, HOLD
muted.
