// In-memory implementation of docs/api-spec.md for offline demos. The
// simulated live day is the scripted two-straddle day; a cursor into its
// steps stands in for the wall clock and the mock event ticker advances it.

import { ApiError } from '@/api/client'
import type {
  Bar,
  Bars,
  BrainState,
  Chain,
  ChainRow,
  Circuits,
  DataStatus,
  Experiment,
  ExperimentConfig,
  Replay,
  ReplayListItem,
  ReplayRunBody,
  Session,
  Settings,
  SettingsPatch,
  Status,
  Straddle,
  WorkerStartBody,
} from '@/api/types'
import { istIso } from '@/lib/time'
import { mockBus } from './bus'
import { DEFAULT_DAY, LOT_SIZE, type MockDay, mockDay, mockDayFor, STEP_COUNT } from './day'
import { MOCK_EXPERIMENTS, summaries } from './experiments'
import { blackPrices, round, round2 } from './random'

const FLAT: Straddle = {
  in_position: false,
  expiry: null,
  strike: null,
  lots: null,
  legs: null,
  entry_credit: null,
  combined_ltp: null,
  stop_level: null,
  target_level: null,
  pnl: null,
  entered_at: null,
  square_off_at: null,
}

export const REPLAY_DATES = [
  '2026-09-11',
  '2026-09-10',
  '2026-09-09',
  '2026-09-08',
  '2026-09-04',
  '2026-09-03',
  '2026-09-02',
  '2026-09-01',
]

interface MockState {
  cursor: number
  worker: Status['worker']
  analyzer: boolean
  settings: Settings
  data: DataStatus
  replays: Map<string, Replay>
  experiments: Experiment[]
  squaredOffAt: number | null
  preparing: boolean
}

function defaultSettings(): Settings {
  return {
    strategy: {
      underlying: 'NIFTY',
      lot_size: LOT_SIZE,
      lots: 1,
      product: 'NRML',
      leg_stop_pct: 30,
      leg_stop_mode: 'broker',
      on_leg_stop: 'hold_other',
      combined_stop_enabled: true,
      stop_pct: 25,
      target_pct: 40,
      lock_after_pct: 15,
      trade_start: '09:20',
      last_entry: '14:30',
      square_off: '15:15',
      max_entries_per_day: 10,
      reentry_cooldown_minutes: 5,
      vix_ceiling: 20,
      min_days_to_expiry: 0,
    },
    risk: {
      daily_loss_limit_pct: 1.0,
      risk_budget_pct: 1.0,
      max_lots: 3,
      spread_pct_max: 0.5,
      quote_max_age_s: 5,
      index_move_veto_pct: 0.3,
    },
    neural: {
      neural_ms: 200,
      encoder: 'B',
      readout: 'reservoir',
      plastic: false,
      live_interval: '1m',
    },
    costs: {
      brokerage_per_order: 20,
      brokerage_pct: 0.03,
      stt_sell_pct: 0.1,
      exchange_pct: 0.03503,
      sebi_pct: 0.0001,
      stamp_buy_pct: 0.003,
      gst_pct: 18,
    },
    openalgo: { host: 'http://127.0.0.1:5000', ws_url: 'ws://127.0.0.1:8765', api_key_set: true },
  }
}

function defaultData(): DataStatus {
  return {
    stage: 'compiled',
    files: [
      {
        name: 'body-annotations-male-cns-v1.0-minconf-0.5.feather',
        present: true,
        verified: true,
        bytes: 14483314,
      },
      {
        name: 'connectome-weights-male-cns-v1.0-minconf-0.5.feather',
        present: true,
        verified: true,
        bytes: 1051241946,
      },
      { name: 'neuron-types-male-cns-v1.0.feather', present: true, verified: true, bytes: 8212944 },
    ],
    graph: { present: true, neurons: 166700, edges: 25582938, verified: true },
    progress: null,
  }
}

function buildReplay(
  id: string,
  day: MockDay,
  config: Omit<ReplayRunBody, 'date'>,
  state: Replay['state']
): Replay {
  const last = day.steps[day.steps.length - 1]
  const trades = day.steps.filter((s) => s.action === 'ENTER' || s.action === 'REENTRY').length
  return {
    id,
    date: day.config.date,
    state,
    config,
    summary: {
      pnl: last.pnl_day,
      trades,
      stop_hits: day.steps.filter((s) => s.action === 'STOP').length,
      target_hits: day.steps.filter((s) => s.action === 'TARGET').length,
      leg_stop_hits: day.steps.filter((s) => s.action === 'STOP_LEG').length,
    },
    progress: { done: STEP_COUNT, total: STEP_COUNT },
    steps: state === 'done' ? day.steps : [],
  }
}

function initialReplays(): Map<string, Replay> {
  const map = new Map<string, Replay>()
  const base: Omit<ReplayRunBody, 'date'> = {
    encoder: 'B',
    readout: 'reservoir',
    neural_ms: 200,
    lots: 1,
    stop_pct: 25,
    target_pct: 40,
    experiment_id: 'exp_20260912_193000_b_reservoir',
  }
  map.set(
    'rp_20260911_101500_b_reservoir',
    buildReplay('rp_20260911_101500_b_reservoir', mockDay(), base, 'done')
  )
  map.set(
    'rp_20260910_224000_b_reservoir',
    buildReplay('rp_20260910_224000_b_reservoir', mockDayFor('2026-09-10'), base, 'done')
  )
  return map
}

export const mockState: MockState = {
  cursor: 200,
  worker: {
    state: 'running',
    mode: 'paper',
    run_dir: 'runs/paper-2026-09-11',
    started_at: istIso(DEFAULT_DAY.date, '09:12', 31),
    last_event_at: null,
  },
  analyzer: true,
  settings: defaultSettings(),
  data: defaultData(),
  replays: initialReplays(),
  experiments: MOCK_EXPERIMENTS,
  squaredOffAt: null,
  preparing: false,
}

export function currentStep() {
  return mockDay().steps[mockState.cursor]
}

export function advanceCursor() {
  mockState.cursor = (mockState.cursor + 1) % STEP_COUNT
  if (mockState.cursor === 0) mockState.squaredOffAt = null
  mockState.worker.last_event_at = currentStep().t
  return currentStep()
}

function session(date = DEFAULT_DAY.date): Session {
  const step = currentStep()
  return {
    trading_date: date,
    is_trading_day: true,
    trade_start: istIso(date, mockState.settings.strategy.trade_start),
    last_entry: istIso(date, mockState.settings.strategy.last_entry),
    square_off: istIso(date, mockState.settings.strategy.square_off),
    is_expiry_day: date === DEFAULT_DAY.expiry,
    now: date === DEFAULT_DAY.date ? step.t : istIso(date, '15:30'),
  }
}

function status(): Status {
  return {
    version: '0.1.0-mock',
    python: '3.12.6',
    numba: '0.61.2',
    data: {
      ready: mockState.data.stage === 'compiled',
      neurons: mockState.data.graph.neurons,
      edges: mockState.data.graph.edges,
      graph_path: mockState.data.graph.present ? 'data/graph.npz' : null,
    },
    openalgo: {
      reachable: true,
      host: mockState.settings.openalgo.host,
      analyzer_mode: mockState.analyzer,
      broker: 'zerodha',
    },
    worker: { ...mockState.worker },
    session: session(),
    live_allowed: false,
  }
}

function currentStraddle(): Straddle {
  if (mockState.worker.state !== 'running') return FLAT
  const day = mockDay()
  const straddle = day.straddleAt(mockState.cursor)
  if (straddle.in_position && mockState.squaredOffAt !== null) {
    // Squared off by hand: stay flat until the day's next entry.
    const enteredAfter = day.steps
      .slice(mockState.squaredOffAt + 1, mockState.cursor + 1)
      .some((s) => s.action === 'ENTER' || s.action === 'REENTRY')
    if (!enteredAfter) return FLAT
  }
  return straddle
}

function aggregate(bars: Bar[], minutes: number): Bar[] {
  if (minutes <= 1) return bars
  const out: Bar[] = []
  for (let i = 0; i < bars.length; i += minutes) {
    const chunk = bars.slice(i, i + minutes)
    out.push({
      t: chunk[0].t,
      o: chunk[0].o,
      h: Math.max(...chunk.map((b) => b.h)),
      l: Math.min(...chunk.map((b) => b.l)),
      c: chunk[chunk.length - 1].c,
      v: chunk.reduce((s, b) => s + b.v, 0),
    })
  }
  return out
}

function bars(params: URLSearchParams): Bars {
  const interval = params.get('interval') ?? '1m'
  const days = Math.max(1, Math.min(5, Number(params.get('days') ?? 1)))
  const symbol = params.get('symbol') ?? 'NIFTY'
  const minutes = Number.parseInt(interval, 10) || 1
  const today = mockDay().bars.slice(0, mockState.cursor + 1)
  const previous: Bar[] = []
  for (let d = 1; d < days; d++) {
    const date = REPLAY_DATES[d]
    if (date) previous.unshift(...mockDayFor(date).bars)
  }
  return {
    symbol,
    exchange: params.get('exchange') ?? 'NSE_INDEX',
    interval,
    bars: aggregate([...previous, ...today], minutes),
  }
}

function chain(): Chain {
  const step = currentStep()
  const forward = step.index + 65
  const atm = Math.round(forward / 50) * 50
  const rows: ChainRow[] = []
  for (let k = -6; k <= 6; k++) {
    const strike = atm + k * 50
    const { ce, pe } = blackPrices(forward, strike, step.days_to_expiry, 0.109)
    const iv = round2(10.9 + Math.abs(k) * 0.12)
    rows.push({
      strike,
      ce: {
        symbol: `NIFTY15SEP26${strike}CE`,
        ltp: round(ce),
        bid: round(ce - 0.3),
        ask: round(ce + 0.35),
        iv,
      },
      pe: {
        symbol: `NIFTY15SEP26${strike}PE`,
        ltp: round(pe),
        bid: round(pe - 0.3),
        ask: round(pe + 0.35),
        iv,
      },
    })
  }
  return {
    underlying: 'NIFTY',
    expiry: DEFAULT_DAY.expiry,
    days_to_expiry: step.days_to_expiry,
    index_ltp: step.index,
    vix: step.vix,
    synthetic_forward: round2(forward),
    atm_strike: atm,
    lot_size: LOT_SIZE,
    rows,
  }
}

function circuits(): Circuits {
  return {
    n: 166700,
    populations: [
      { name: 'KC', size: 5000 },
      { name: 'MBON', size: 96 },
      { name: 'DN', size: 1300 },
      { name: 'DNp20_L', size: 1 },
      { name: 'DNp20_R', size: 1 },
      { name: 'DNpe017', size: 1 },
      { name: 'PAM', size: 260 },
      { name: 'PPL1', size: 24 },
      { name: 'LC', size: 900 },
      { name: 'RANDOM', size: 2000 },
    ],
  }
}

function brainState(): BrainState {
  const step = currentStep()
  return {
    observed_at: step.t,
    neural_ms: mockState.settings.neural.neural_ms,
    sim_ms: (step.i + 1) * mockState.settings.neural.neural_ms,
    compute_seconds: step.compute_seconds,
    rates_hz: step.rates_hz,
    fixed_decoder: step.fixed_decoder,
    prediction: step.prediction,
    stimulus_hash: step.stimulus_hash,
    plastic: mockState.settings.neural.plastic
      ? { edges: 480000, mean_weight: 0.51, potentiated: 1240, depressed: 980, last_reward: 0.3 }
      : null,
  }
}

export function mockStimulusUrl(): string {
  return currentStep().stimulus_png
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function runDataPipeline() {
  if (mockState.preparing) return
  mockState.preparing = true
  const files = mockState.data.files
  let fileIndex = 0
  let done = 0
  mockState.data.stage = 'downloading'
  const tick = () => {
    const file = files[fileIndex]
    done += Math.max(file.bytes / 12, 1)
    if (done >= file.bytes) {
      done = 0
      fileIndex += 1
    }
    if (fileIndex >= files.length) {
      mockState.data.stage = 'compiling'
      mockState.data.progress = {
        stage: 'compile',
        file: null,
        done_bytes: null,
        total_bytes: null,
        message: 'compiling sparse graph',
      }
      mockBus.emit('data.progress', mockState.data.progress)
      window.setTimeout(() => {
        mockState.data.stage = 'compiled'
        mockState.data.progress = {
          stage: 'compiled',
          file: null,
          done_bytes: null,
          total_bytes: null,
          message: 'graph verified',
        }
        mockState.data.graph = { present: true, neurons: 166700, edges: 25582938, verified: true }
        mockBus.emit('data.progress', mockState.data.progress)
        mockState.preparing = false
      }, 2500)
      return
    }
    mockState.data.progress = {
      stage: 'download',
      file: files[fileIndex].name,
      done_bytes: Math.round(done),
      total_bytes: files[fileIndex].bytes,
    }
    mockBus.emit('data.progress', mockState.data.progress)
    window.setTimeout(tick, 350)
  }
  window.setTimeout(tick, 200)
}

function runReplay(body: ReplayRunBody): string {
  const stamp = new Date()
    .toISOString()
    .replace(/[-:TZ.]/g, '')
    .slice(0, 14)
  const id = `rp_${body.date.replace(/-/g, '')}_${stamp}_${body.encoder.toLowerCase()}_${body.readout}`
  const config: Omit<ReplayRunBody, 'date'> = {
    encoder: body.encoder,
    readout: body.readout,
    neural_ms: body.neural_ms,
    lots: body.lots,
    stop_pct: body.stop_pct,
    target_pct: body.target_pct,
    experiment_id: body.experiment_id,
  }
  const overrides = {
    encoder: body.encoder,
    readout: body.readout,
    neural_ms: body.neural_ms,
    lots: body.lots,
    stop_pct: body.stop_pct,
    target_pct: body.target_pct,
  }
  const day =
    body.date === DEFAULT_DAY.date &&
    body.stop_pct === 25 &&
    body.target_pct === 40 &&
    body.lots === 1
      ? mockDayFor(body.date, body.encoder === 'B' && body.neural_ms === 200 ? {} : overrides)
      : mockDayFor(body.date, overrides)
  const replay = buildReplay(id, day, config, 'running')
  replay.progress = { done: 0, total: STEP_COUNT }
  mockState.replays.set(id, replay)
  let done = 0
  const tick = () => {
    done = Math.min(STEP_COUNT, done + 45)
    replay.progress = { done, total: STEP_COUNT }
    mockBus.emit('replay.progress', { id, done, total: STEP_COUNT })
    if (done >= STEP_COUNT) {
      replay.state = 'done'
      replay.steps = day.steps
      return
    }
    window.setTimeout(tick, 300)
  }
  window.setTimeout(tick, 300)
  return id
}

function createExperiment(config: ExperimentConfig): string {
  const stamp = new Date()
    .toISOString()
    .replace(/[-:TZ.]/g, '')
    .slice(0, 14)
  const id = `exp_${stamp}_${config.encoder.toLowerCase()}_${config.readout}`
  const total = 21000
  const experiment: Experiment = {
    id,
    name:
      config.name ??
      `encoder ${config.encoder}, ${config.readout}, ${config.plastic ? 'plastic' : 'frozen'}`,
    config,
    state: 'running',
    created_at: new Date().toISOString(),
    progress: { done: 0, total },
    metrics: {},
    controls: {},
    curves: {},
    passed: null,
    verdict: '',
  }
  mockState.experiments = [experiment, ...mockState.experiments]
  let done = 0
  const tick = () => {
    done = Math.min(total, done + 700)
    experiment.progress = { done, total }
    mockBus.emit('experiment.progress', { id, done, total })
    if (done >= total) {
      const template = MOCK_EXPERIMENTS[0]
      experiment.state = 'done'
      experiment.metrics = template.metrics
      experiment.controls = template.controls
      experiment.curves = template.curves
      experiment.passed = false
      experiment.verdict =
        'no edge found: accuracy 0.50 within bootstrap interval of 0.5 (p = 0.48)'
      return
    }
    window.setTimeout(tick, 400)
  }
  window.setTimeout(tick, 400)
  return id
}

function mergeSettings(patch: SettingsPatch): Settings {
  const s = mockState.settings
  const next: Settings = {
    strategy: { ...s.strategy, ...(patch.strategy ?? {}) },
    risk: { ...s.risk, ...(patch.risk ?? {}) },
    neural: { ...s.neural, ...(patch.neural ?? {}) },
    costs: { ...s.costs, ...(patch.costs ?? {}) },
    openalgo: {
      host: patch.openalgo?.host ?? s.openalgo.host,
      ws_url: patch.openalgo?.ws_url ?? s.openalgo.ws_url,
      api_key_set: patch.openalgo?.api_key ? true : s.openalgo.api_key_set,
    },
  }
  mockState.settings = next
  return next
}

function workerStart(body: WorkerStartBody) {
  if (body.mode === 'paper' && !mockState.analyzer) {
    throw new ApiError(409, 'paper mode requires the OpenAlgo analyzer to be on')
  }
  if (body.mode === 'live') {
    throw new ApiError(
      403,
      'live mode requires OPENFLY_LIVE=I_ACCEPT_REAL_TRADES and a passed experiment'
    )
  }
  if (mockState.worker.state === 'running') {
    throw new ApiError(409, 'worker already running')
  }
  mockState.worker = {
    state: 'starting',
    mode: body.mode,
    run_dir: body.run_dir ?? `runs/${body.mode}-${DEFAULT_DAY.date}`,
    started_at: new Date().toISOString(),
    last_event_at: null,
  }
  mockBus.emit('worker', { ...mockState.worker })
  window.setTimeout(() => {
    mockState.worker.state = 'running'
    mockBus.emit('worker', { ...mockState.worker })
  }, 1200)
  return { state: mockState.worker.state, run_dir: mockState.worker.run_dir ?? '' }
}

export async function mockRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  await delay(60 + Math.random() * 120)
  const url = new URL(path, 'http://mock.local')
  const p = url.pathname
  const q = url.searchParams
  const respond = (value: unknown) => structuredClone(value) as T

  if (method === 'GET' && p === '/api/status') return respond(status())
  if (method === 'GET' && p === '/api/data/status') return respond(mockState.data)
  if (method === 'POST' && p === '/api/data/prepare') {
    runDataPipeline()
    return respond({ started: true })
  }
  if (method === 'GET' && p === '/api/market/bars') return respond(bars(q))
  if (method === 'GET' && p === '/api/market/chain') return respond(chain())
  if (method === 'GET' && p === '/api/market/session')
    return respond(session(q.get('date') ?? DEFAULT_DAY.date))
  if (method === 'GET' && p === '/api/brain/circuits') return respond(circuits())
  if (method === 'GET' && p === '/api/brain/state') return respond(brainState())
  if (method === 'GET' && p === '/api/experiments') {
    return respond({
      experiments: summaries().length
        ? mockState.experiments.map((e) => ({
            id: e.id,
            name: e.name ?? e.id,
            state: e.state,
            created_at: e.created_at ?? '',
            config: e.config,
            progress: e.progress ?? null,
          }))
        : [],
    })
  }
  if (method === 'POST' && p === '/api/experiments') {
    return respond({ id: createExperiment(body as ExperimentConfig) })
  }
  const experimentMatch = p.match(/^\/api\/experiments\/([^/]+)$/)
  if (method === 'GET' && experimentMatch) {
    const experiment = mockState.experiments.find(
      (e) => e.id === decodeURIComponent(experimentMatch[1])
    )
    if (!experiment) throw new ApiError(404, 'experiment not found')
    return respond(experiment)
  }
  if (method === 'POST' && p === '/api/worker/start')
    return respond(workerStart(body as WorkerStartBody))
  if (method === 'POST' && p === '/api/worker/stop') {
    mockState.worker = {
      state: 'stopped',
      mode: null,
      run_dir: null,
      started_at: null,
      last_event_at: null,
    }
    mockBus.emit('worker', { ...mockState.worker })
    return respond({ ok: true })
  }
  if (method === 'POST' && p === '/api/worker/squareoff') {
    mockState.squaredOffAt = mockState.cursor
    mockBus.emit('straddle', FLAT)
    mockBus.emit('log', {
      level: 'info',
      message: 'manual square off: exit basket sent for both legs',
    })
    return respond({ ok: true })
  }
  if (method === 'GET' && p === '/api/straddle') return respond(currentStraddle())
  if (method === 'GET' && p === '/api/ledger/intents')
    return respond({ intents: [...mockDay().intentsAt(mockState.cursor)].reverse() })
  if (method === 'GET' && p === '/api/orders')
    return respond({ orders: [...mockDay().ordersAt(mockState.cursor)].reverse() })
  if (method === 'GET' && p === '/api/positions')
    return respond({ positions: mockDay().positionsAt(mockState.cursor) })
  if (method === 'GET' && p === '/api/settings') return respond(mockState.settings)
  if (method === 'PUT' && p === '/api/settings')
    return respond(mergeSettings((body ?? {}) as SettingsPatch))
  if (method === 'POST' && p === '/api/settings/analyzer') {
    mockState.analyzer = Boolean((body as { mode: boolean }).mode)
    return respond({ analyzer_mode: mockState.analyzer })
  }
  if (method === 'GET' && p === '/api/replay/dates')
    return respond({ dates: REPLAY_DATES, source: 'history cache (mock)' })
  if (method === 'GET' && p === '/api/replay') {
    const replays: ReplayListItem[] = [...mockState.replays.values()]
      .map(({ steps: _steps, ...rest }) => rest)
      .sort((a, b) => (a.id < b.id ? 1 : -1))
    return respond({ replays })
  }
  if (method === 'POST' && p === '/api/replay/run')
    return respond({ id: runReplay(body as ReplayRunBody) })
  const replayMatch = p.match(/^\/api\/replay\/([^/]+)$/)
  if (method === 'GET' && replayMatch) {
    const replay = mockState.replays.get(decodeURIComponent(replayMatch[1]))
    if (!replay) throw new ApiError(404, 'replay not found')
    return respond(replay)
  }
  throw new ApiError(404, `mock: no route for ${method} ${p}`)
}
