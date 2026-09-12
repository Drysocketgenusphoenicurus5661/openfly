// Runtime type guards for the JSON shapes we rely on most. They keep bad
// payloads (a half-written backend, a proxy error page) from crashing the UI.

import {
  ACTIONS,
  type Action,
  type ReplayStep,
  type ServerEvent,
  type Status,
  type Straddle,
} from './types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export function isAction(value: unknown): value is Action {
  return typeof value === 'string' && (ACTIONS as string[]).includes(value)
}

export function isServerEvent(value: unknown): value is ServerEvent {
  return isRecord(value) && typeof value.type === 'string' && 'data' in value
}

export function isStatus(value: unknown): value is Status {
  if (!isRecord(value)) return false
  const { data, openalgo, worker, session } = value
  return (
    typeof value.version === 'string' &&
    isRecord(data) &&
    typeof data.ready === 'boolean' &&
    isRecord(openalgo) &&
    typeof openalgo.reachable === 'boolean' &&
    isRecord(worker) &&
    typeof worker.state === 'string' &&
    isRecord(session) &&
    typeof session.trading_date === 'string'
  )
}

export function isStraddle(value: unknown): value is Straddle {
  if (!isRecord(value) || typeof value.in_position !== 'boolean') return false
  if (!value.in_position) return true
  return (
    isNumber(value.strike) &&
    Array.isArray(value.legs) &&
    isNumber(value.entry_credit) &&
    isNumber(value.combined_ltp) &&
    isNumber(value.stop_level) &&
    isNumber(value.target_level)
  )
}

export function isReplayStep(value: unknown): value is ReplayStep {
  if (!isRecord(value)) return false
  return (
    isNumber(value.i) &&
    typeof value.t === 'string' &&
    isNumber(value.index) &&
    isNumber(value.premium) &&
    isRecord(value.rates_hz) &&
    isRecord(value.prediction) &&
    isRecord(value.guard) &&
    Array.isArray((value.guard as Record<string, unknown>).checks) &&
    isAction(value.action) &&
    isRecord(value.straddle) &&
    typeof value.narrative === 'string' &&
    isRecord(value.technical)
  )
}

export function isReplayStepArray(value: unknown): value is ReplayStep[] {
  return Array.isArray(value) && value.every(isReplayStep)
}
