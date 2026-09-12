import { describe, expect, it } from 'vitest'
import { mockDay } from '@/mock/day'
import {
  isAction,
  isReplayStep,
  isReplayStepArray,
  isServerEvent,
  isStatus,
  isStraddle,
} from './guards'

const status = {
  version: '0.1.0',
  python: '3.11.16',
  data: { ready: true, neurons: 166700, edges: 25582938, graph_path: 'data/graph.npz' },
  openalgo: {
    reachable: true,
    host: 'http://127.0.0.1:5000',
    analyzer_mode: true,
    broker: 'zerodha',
  },
  worker: { state: 'stopped', mode: null, run_dir: null, started_at: null, last_event_at: null },
  session: {
    trading_date: '2026-09-15',
    is_trading_day: true,
    trade_start: '2026-09-15T09:20:00+05:30',
    last_entry: '2026-09-15T14:30:00+05:30',
    square_off: '2026-09-15T15:15:00+05:30',
    is_expiry_day: true,
    now: '2026-09-15T10:04:12+05:30',
  },
}

describe('API type guards', () => {
  it('accepts the status shape from the spec and rejects an error page', () => {
    expect(isStatus(status)).toBe(true)
    expect(isStatus({ detail: 'not found' })).toBe(false)
    expect(isStatus(null)).toBe(false)
    expect(isStatus('<html></html>')).toBe(false)
  })

  it('accepts every generated replay step and rejects broken ones', () => {
    const steps = mockDay().steps
    expect(steps).toHaveLength(375)
    expect(isReplayStepArray(steps)).toBe(true)
    const broken = { ...steps[0], action: 'DANCE' }
    expect(isReplayStep(broken)).toBe(false)
    const missingNarrative = { ...steps[0], narrative: undefined }
    expect(isReplayStep(missingNarrative)).toBe(false)
    expect(isReplayStep({ i: 1 })).toBe(false)
  })

  it('knows the action vocabulary', () => {
    for (const a of [
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
    ]) {
      expect(isAction(a)).toBe(true)
    }
    expect(isAction('BUY')).toBe(false)
  })

  it('checks straddle payloads flat and in position', () => {
    expect(isStraddle({ in_position: false, strike: null })).toBe(true)
    expect(isStraddle(mockDay().straddleAt(200))).toBe(true)
    expect(isStraddle({ in_position: true, strike: 'x' })).toBe(false)
  })

  it('checks websocket envelopes', () => {
    expect(isServerEvent({ type: 'observation', at: 'now', data: {} })).toBe(true)
    expect(isServerEvent({ type: 'pong' })).toBe(false)
    expect(isServerEvent('nope')).toBe(false)
  })
})
