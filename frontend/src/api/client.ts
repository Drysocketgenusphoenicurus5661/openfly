// Fetch-based client for every endpoint in docs/api-spec.md.

import { useBackendStore } from './backend'
import type {
  AnalyzerResponse,
  Bars,
  BrainState,
  Chain,
  Circuits,
  DataStatus,
  Experiment,
  ExperimentConfig,
  ExperimentList,
  IdResponse,
  IntentList,
  OkResponse,
  OrderList,
  PositionList,
  Replay,
  ReplayDates,
  ReplayList,
  ReplayRunBody,
  Session,
  Settings,
  SettingsPatch,
  StartedResponse,
  Status,
  Straddle,
  WorkerStartBody,
  WorkerStartResponse,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'

async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    // A network failure means the backend may be gone: let the probe decide.
    void useBackendStore.getState().probe()
    throw new ApiError(
      0,
      `Backend unreachable: ${error instanceof Error ? error.message : String(error)}`
    )
  }
  const text = await response.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = null
    }
  }
  if (!response.ok) {
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`
    throw new ApiError(response.status, detail)
  }
  return parsed as T
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  status: () => request<Status>('GET', '/api/status'),

  dataStatus: () => request<DataStatus>('GET', '/api/data/status'),
  dataPrepare: () => request<StartedResponse>('POST', '/api/data/prepare'),

  bars: (params: { symbol?: string; exchange?: string; interval?: string; days?: number } = {}) =>
    request<Bars>(
      'GET',
      `/api/market/bars${query({
        symbol: params.symbol ?? 'NIFTY',
        exchange: params.exchange ?? 'NSE_INDEX',
        interval: params.interval ?? '1m',
        days: params.days ?? 1,
      })}`
    ),
  chain: () => request<Chain>('GET', '/api/market/chain'),
  session: (date?: string) => request<Session>('GET', `/api/market/session${query({ date })}`),

  circuits: () => request<Circuits>('GET', '/api/brain/circuits'),
  brainState: () => request<BrainState>('GET', '/api/brain/state'),
  stimulusUrl: () => '/api/brain/stimulus.png',

  experiments: () => request<ExperimentList>('GET', '/api/experiments'),
  experiment: (id: string) =>
    request<Experiment>('GET', `/api/experiments/${encodeURIComponent(id)}`),
  createExperiment: (config: ExperimentConfig) =>
    request<IdResponse>('POST', '/api/experiments', config),

  workerStart: (body: WorkerStartBody) =>
    request<WorkerStartResponse>('POST', '/api/worker/start', body),
  workerStop: () => request<OkResponse>('POST', '/api/worker/stop'),
  workerSquareOff: () => request<OkResponse>('POST', '/api/worker/squareoff'),

  straddle: () => request<Straddle>('GET', '/api/straddle'),
  intents: () => request<IntentList>('GET', '/api/ledger/intents'),
  orders: () => request<OrderList>('GET', '/api/orders'),
  positions: () => request<PositionList>('GET', '/api/positions'),

  settings: () => request<Settings>('GET', '/api/settings'),
  updateSettings: (patch: SettingsPatch) => request<Settings>('PUT', '/api/settings', patch),
  setAnalyzer: (mode: boolean) =>
    request<AnalyzerResponse>('POST', '/api/settings/analyzer', { mode }),

  replayDates: () => request<ReplayDates>('GET', '/api/replay/dates'),
  replays: () => request<ReplayList>('GET', '/api/replay'),
  replay: (id: string) => request<Replay>('GET', `/api/replay/${encodeURIComponent(id)}`),
  runReplay: (body: ReplayRunBody) => request<IdResponse>('POST', '/api/replay/run', body),
  replayStimulusUrl: (id: string, i: number) =>
    `/api/replay/${encodeURIComponent(id)}/stimulus/${i}.png`,
}

export type Api = typeof api
