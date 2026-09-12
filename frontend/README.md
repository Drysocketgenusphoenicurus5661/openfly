# OpenFly web frontend

The browser UI for OpenFly: what the fly saw, what it spiked, what the
readout predicted, what the guard did, the straddle against its stop and
target, and P&L against the controls. Built with Vite, React 19,
TypeScript, Tailwind v4, shadcn (new-york, neutral), openalgo-charts,
TanStack Query, zustand and react-router. Every page reads the real API
described in docs/api-spec.md; there is no generated data.

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

## Backend not running

At startup the app probes `GET /api/status`. While the backend is down it
shows one full-page state ("OpenFly backend is not running. Start it with:
uv run app.py") with a retry button and retries on its own every ten
seconds. A network failure on any later request, or the event websocket
dropping, triggers the same probe, so the page reappears if the backend
goes away and clears when it is back. Pages show their own empty states
when the API returns nothing yet: no replays, no experiments, worker
stopped, no observation, flat.

## Layout

```
src/api          typed client (client.ts), types mirroring docs/api-spec.md,
                 type guards, TanStack Query hooks, the /api/events hook
                 with reconnect and a 500 event ring buffer, backend probe
src/components   charts (openalgo-charts wrappers: two titled panes, price
                 lines, markers, replay), decision panel, brain widgets,
                 replay widgets, layout (sidebar, header, session clock,
                 kill switch), shadcn ui
src/pages        Dashboard, Brain, Replay, Experiments, Orders, Settings, Setup
src/stores       zustand stores (replay player)
src/hooks        latest decision, session clock, replay clock, virtual rows
src/lib          formatting, IST time helpers, action colours, chart data,
                 stop basis wording
```

## Conventions

Plain text everywhere: no emoji, no icons in text, no em or en dashes.
lucide icons appear only in navigation and on buttons. Numbers use
tabular figures. Actions are colour coded the same way on badges, the
timeline and chart markers: ENTER and REENTRY green, EXIT and TARGET blue,
STOP and STOP_LEG red, SQUARE_OFF orange, LOCK violet, VETO grey, HOLD
muted. The premium pane carries a "recorded premium" or "synthetic
premium" badge whenever the API reports `premium_source`.
