// TypeScript mirrors of the JSON shapes in docs/api-spec.md. Field names
// match the wire format exactly; do not rename them here.

export type WorkerState = 'stopped' | 'starting' | 'running' | 'halted'
export type WorkerMode = 'paper' | 'live'

export interface Session {
  trading_date: string
  is_trading_day: boolean
  trade_start: string
  last_entry: string
  square_off: string
  is_expiry_day: boolean
  now: string
}

export interface Status {
  version: string
  python: string
  data: { ready: boolean; neurons: number; edges: number; graph_path: string | null }
  openalgo: {
    reachable: boolean
    host: string
    analyzer_mode: boolean
    broker: string | null
  }
  worker: {
    state: WorkerState
    mode: WorkerMode | null
    run_dir: string | null
    started_at: string | null
    last_event_at: string | null
  }
  session: Session
  // Optional extras a backend may report. The UI treats them as unknown when absent.
  numba?: string | boolean | null
  live_allowed?: boolean
}

export type DataStage =
  | 'missing'
  | 'downloading'
  | 'downloaded'
  | 'compiling'
  | 'compiled'
  | 'error'

export interface DataFile {
  name: string
  present: boolean
  verified: boolean
  bytes: number
}

export interface DataProgress {
  stage: string
  file?: string | null
  done_bytes?: number | null
  total_bytes?: number | null
  message?: string | null
}

export interface DataStatus {
  stage: DataStage
  files: DataFile[]
  graph: { present: boolean; neurons: number; edges: number; verified: boolean }
  progress: DataProgress | null
  error?: string | null
}

export interface Bar {
  t: string
  o: number
  h: number
  l: number
  c: number
  v: number
}

export interface Bars {
  symbol: string
  exchange: string
  interval: string
  bars: Bar[]
}

export interface ChainQuote {
  symbol: string
  ltp: number
  bid: number
  ask: number
  iv: number
}

export interface ChainRow {
  strike: number
  ce: ChainQuote
  pe: ChainQuote
}

export interface Chain {
  underlying: string
  expiry: string
  days_to_expiry: number
  index_ltp: number
  vix: number
  synthetic_forward: number
  atm_strike: number
  lot_size: number
  rows: ChainRow[]
}

export interface Population {
  name: string
  size: number
}

export interface Circuits {
  n: number
  populations: Population[]
}

export type RatesHz = Record<string, number>

export interface FixedDecoder {
  left_hz: number
  right_hz: number
  difference_hz: number
  gate_spikes: number
  side: string
}

export interface Prediction {
  realized_over_implied: number
  confidence: number
  decision: string
  tau?: number
}

export interface BrainState {
  observed_at: string
  neural_ms: number
  sim_ms: number
  compute_seconds: number
  rates_hz: RatesHz
  fixed_decoder: FixedDecoder
  prediction: Prediction
  stimulus_hash: string
  plastic?: PlasticStats | null
}

export interface PlasticStats {
  edges: number
  mean_weight: number
  potentiated: number
  depressed: number
  last_reward: number | null
}

export type Encoder = 'A' | 'B' | 'C'
export type Readout = 'fixed' | 'reservoir' | 'plastic'

export interface ExperimentConfig {
  encoder: Encoder
  readout: Readout
  plastic: boolean
  neural_ms: number
  train: [string, string]
  validation: [string, string]
  test: [string, string]
  name?: string
}

export type ExperimentState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export interface ExperimentSummary {
  id: string
  name: string
  state: ExperimentState
  created_at: string
  config: ExperimentConfig
  progress: { done: number; total: number } | null
}

export interface ExperimentMetrics {
  net_pnl_per_lot: number
  sharpe: number
  max_drawdown: number
  trades: number
  stop_hits: number
  target_hits: number
  accuracy: number
  accuracy_ci: [number, number]
}

export type ControlName = 'fixed_0920' | 'random_entry' | 'shuffled' | 'flat'

export interface ExperimentCurve {
  t: string[]
  strategy: number[]
  fixed_0920?: number[]
  random_entry?: number[]
  shuffled?: number[]
  flat?: number[]
}

export interface Experiment {
  id: string
  name?: string
  config: ExperimentConfig
  state: ExperimentState
  created_at?: string
  progress?: { done: number; total: number } | null
  metrics: Partial<Record<'train' | 'validation' | 'test', ExperimentMetrics>>
  controls: Partial<Record<ControlName, ExperimentMetrics>>
  curves: Partial<Record<'train' | 'validation' | 'test', ExperimentCurve>>
  passed: boolean | null
  verdict: string
}

export interface ExperimentList {
  experiments: ExperimentSummary[]
}

export interface WorkerStartBody {
  mode: WorkerMode
  lots: number
  run_dir: string | null
}

export interface WorkerStartResponse {
  state: WorkerState
  run_dir: string
}

export interface OkResponse {
  ok: boolean
}

export type LegStopStatus = 'pending' | 'complete' | 'cancelled' | 'rejected' | 'none'
export type LegStatus = 'open' | 'stopped' | 'closed'

export interface StraddleLeg {
  symbol: string
  side: 'SELL' | 'BUY'
  qty: number
  entry_price: number
  ltp: number
  stop_price?: number | null
  stop_order_id?: string | null
  stop_status?: LegStopStatus
  status?: LegStatus
}

export interface StraddleInPosition {
  in_position: true
  expiry: string
  strike: number
  lots: number
  legs: StraddleLeg[]
  entry_credit: number
  combined_ltp: number
  stop_level: number
  target_level: number
  pnl: number
  entered_at: string
  square_off_at: string
}

export interface StraddleFlat {
  in_position: false
  expiry: null
  strike: null
  lots: null
  legs: null
  entry_credit: null
  combined_ltp: null
  stop_level: null
  target_level: null
  pnl: null
  entered_at: null
  square_off_at: null
}

export type Straddle = StraddleInPosition | StraddleFlat

export type IntentStatus = 'PREPARED' | 'UNKNOWN' | 'ACCEPTED' | 'PARTIAL' | 'SETTLED' | 'REJECTED'
export type IntentKind = 'ENTRY' | 'EXIT' | 'STOP' | 'STOP_LEG' | 'SQUARE_OFF' | 'UNWIND' | string

export interface IntentLeg {
  symbol: string
  side: 'SELL' | 'BUY'
  quantity: number
  order_id: string | null
  status: string
  average_price: number | null
}

export interface Intent {
  intent_id: string
  kind: IntentKind
  status: IntentStatus
  created_at: string
  reason: string
  legs: IntentLeg[]
}

export interface IntentList {
  intents: Intent[]
}

// OpenAlgo orderbook row, field names as OpenAlgo returns them.
export interface OpenAlgoOrder {
  orderid: string
  symbol: string
  exchange: string
  action: string
  quantity: number | string
  price: number | string
  trigger_price: number | string
  pricetype: string
  product: string
  order_status: string
  timestamp: string | null
  average_price?: number | string | null
  filled_quantity?: number | string | null
  strategy?: string | null
}

export interface OrderList {
  orders: OpenAlgoOrder[]
}

// OpenAlgo positionbook row. Money and quantity fields may arrive as strings.
export interface OpenAlgoPosition {
  symbol: string
  exchange: string
  product: string
  quantity: number | string
  average_price: number | string
  ltp: number | string
  pnl: number | string
  pnlpercent?: number | string
}

export interface PositionList {
  positions: OpenAlgoPosition[]
}

export type LegStopMode = 'broker' | 'software'
export type OnLegStop = 'hold_other' | 'exit_both'

export interface StrategySettings {
  underlying: string
  lot_size: number
  lots: number
  product: string
  leg_stop_pct: number
  leg_stop_mode: LegStopMode
  on_leg_stop: OnLegStop
  combined_stop_enabled: boolean
  stop_pct: number
  target_pct: number
  lock_after_pct: number
  trade_start: string
  last_entry: string
  square_off: string
  max_entries_per_day: number
  reentry_cooldown_minutes: number
  vix_ceiling: number
  min_days_to_expiry: number
}

export interface RiskSettings {
  daily_loss_limit_pct: number
  risk_budget_pct: number
  max_lots: number
  spread_pct_max: number
  quote_max_age_s: number
  index_move_veto_pct: number
}

export type LiveInterval = '1m' | '3m' | '5m' | '10m' | '15m'
export const LIVE_INTERVALS: LiveInterval[] = ['1m', '3m', '5m', '10m', '15m']

export interface NeuralSettings {
  neural_ms: number
  encoder: Encoder
  readout: Readout
  plastic: boolean
  // How often the fly observes in the live worker.
  live_interval: LiveInterval
}

export interface CostSettings {
  brokerage_per_order: number
  brokerage_pct: number
  stt_sell_pct: number
  exchange_pct: number
  sebi_pct: number
  stamp_buy_pct: number
  gst_pct: number
}

export interface OpenAlgoSettings {
  host: string
  ws_url: string
  api_key_set: boolean
}

export interface Settings {
  strategy: StrategySettings
  risk: RiskSettings
  neural: NeuralSettings
  costs: CostSettings
  openalgo: OpenAlgoSettings
}

// PUT body: any subset. The API key travels only in this direction.
export interface SettingsPatch {
  strategy?: Partial<StrategySettings>
  risk?: Partial<RiskSettings>
  neural?: Partial<NeuralSettings>
  costs?: Partial<CostSettings>
  openalgo?: Partial<Omit<OpenAlgoSettings, 'api_key_set'>> & { api_key?: string }
}

export interface AnalyzerResponse {
  analyzer_mode: boolean
}

export type EventType =
  | 'observation'
  | 'prediction'
  | 'guard'
  | 'straddle'
  | 'intent'
  | 'order'
  | 'reconcile'
  | 'worker'
  | 'data.progress'
  | 'experiment.progress'
  | 'replay.progress'
  | 'log'
  | 'pong'

export interface ServerEvent<T = unknown> {
  type: EventType | string
  at: string
  data: T
}

export type Action =
  | 'ENTER'
  | 'EXIT'
  | 'STOP'
  | 'STOP_LEG'
  | 'TARGET'
  | 'LOCK'
  | 'SQUARE_OFF'
  | 'REENTRY'
  | 'HOLD'
  | 'VETO'
  | 'NONE'

export const ACTIONS: Action[] = [
  'ENTER',
  'EXIT',
  'STOP',
  'STOP_LEG',
  'TARGET',
  'LOCK',
  'SQUARE_OFF',
  'REENTRY',
  'HOLD',
  'VETO',
  'NONE',
]

export interface GuardCheck {
  name: string
  ok: boolean
  detail: string
}

export interface Guard {
  allowed: boolean
  checks: GuardCheck[]
}

export interface StepStraddle {
  in_position: boolean
  strike: number | null
  lots: number | null
  entry_credit: number | null
  combined_ltp: number | null
  stop_level: number | null
  target_level: number | null
  pnl: number | null
  legs?: StraddleLeg[] | null
  expiry?: string | null
}

export interface Fill {
  symbol: string
  side: 'SELL' | 'BUY'
  qty: number
  price: number
}

export interface StepTechnical {
  encoder?: Encoder | string
  readout?: Readout | string
  neural_ms?: number
  features?: number
  top_populations?: [string, number][]
  ridge_alpha?: number
  implied_move_points?: number
  predicted_move_points?: number
  [key: string]: unknown
}

export interface ReplayStep {
  i: number
  t: string
  index: number
  vix: number
  premium: number
  days_to_expiry: number
  stimulus_hash: string
  stimulus_png: string
  rates_hz: RatesHz
  fixed_decoder: FixedDecoder
  prediction: Prediction
  guard: Guard
  action: Action
  straddle: StepStraddle
  fills: Fill[]
  pnl_day: number
  compute_seconds: number
  narrative: string
  technical: StepTechnical
}

export interface ReplayDates {
  dates: string[]
  source: string
}

export interface ReplayRunBody {
  date: string
  encoder: Encoder
  readout: Readout
  neural_ms: number
  lots: number
  stop_pct: number
  target_pct: number
  experiment_id: string | null
}

export type ReplayState = 'queued' | 'running' | 'done' | 'failed'

export interface ReplaySummary {
  pnl: number
  trades: number
  stop_hits?: number
  target_hits?: number
  leg_stop_hits?: number
}

export interface ReplayListItem {
  id: string
  date: string
  state: ReplayState
  config: Omit<ReplayRunBody, 'date'> | Record<string, unknown>
  summary: ReplaySummary | null
  progress?: { done: number; total: number } | null
}

export interface ReplayList {
  replays: ReplayListItem[]
}

export interface Replay extends ReplayListItem {
  steps: ReplayStep[]
}

export interface IdResponse {
  id: string
}

export interface StartedResponse {
  started: boolean
}

export interface ApiErrorBody {
  detail: string
}
