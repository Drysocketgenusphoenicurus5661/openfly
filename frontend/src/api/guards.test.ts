import { describe, expect, it } from 'vitest'
import {
  isAction,
  isReplayStep,
  isReplayStepArray,
  isServerEvent,
  isStatus,
  isStraddle,
} from './guards'
import type { ReplayStep, Straddle } from './types'

const status = {
  version: '0.1.0',
  python: '3.11.16',
  data: { ready: true, neurons: 166700, edges: 25582938, graph_path: 'data/graph.npz' },
  openalgo: {
    reachable: true,
    host: 'http://127.0.0.1:5000',
    analyzer_mode: true,
    broker: 'yourbroker',
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

const step: ReplayStep = {
  i: 12,
  t: '2026-09-11T10:20:00+05:30',
  index: 23350.2,
  vix: 12.1,
  premium: 201.3,
  days_to_expiry: 3.6,
  stimulus_hash: 'sha256:abc',
  stimulus_png: '/api/replay/rp_x/stimulus/12.png',
  rates_hz: { KC: 1.2, MBON: 4.5, DN: 2.1, DNp20_L: 6.0, DNp20_R: 8.0, DNpe017: 2.0 },
  fixed_decoder: { left_hz: 6.0, right_hz: 8.0, difference_hz: 2.0, gate_spikes: 1, side: 'ENTER' },
  prediction: { realized_over_implied: 0.82, confidence: 0.61, decision: 'ENTER', tau: 0.1 },
  guard: {
    allowed: true,
    checks: [{ name: 'trade_window', ok: true, detail: '10:20 within 09:20 to 14:30' }],
  },
  action: 'ENTER',
  straddle: {
    in_position: true,
    strike: 23350,
    lots: 1,
    entry_credit: 201.3,
    combined_ltp: 201.3,
    stop_level: 251.6,
    target_level: 120.8,
    pnl: 0,
    premium_source: 'recorded',
  },
  fills: [{ symbol: 'NIFTY15SEP2623350CE', side: 'SELL', qty: 65, price: 110.2 }],
  pnl_day: 0,
  compute_seconds: 0.4,
  narrative: 'Sold 1 lot of the 23350 straddle for 201.3 points credit.',
  technical: { encoder: 'B', readout: 'reservoir', neural_ms: 200, features: 3500 },
  stop_basis: {
    mode: 'adaptive',
    horizon_minutes: 60,
    expected_move_points: 95,
    implied_move_points: 88,
    realized_move_points: 95,
    leg_stop_pct: { ce: 31.2, pe: 28.7 },
    combined_stop_pct: 18.4,
  },
}

const straddle: Straddle = {
  in_position: true,
  expiry: '2026-09-15',
  strike: 23450,
  lots: 1,
  legs: [
    {
      symbol: 'NIFTY15SEP2623450CE',
      side: 'SELL',
      qty: 65,
      entry_price: 101.2,
      ltp: 95.0,
      stop_price: 131.6,
      stop_order_id: '2509',
      stop_status: 'pending',
      status: 'open',
    },
  ],
  entry_credit: 199.6,
  combined_ltp: 185.1,
  stop_level: 249.5,
  target_level: 119.8,
  pnl: 942.5,
  entered_at: '2026-09-15T10:05:04+05:30',
  square_off_at: '2026-09-15T15:15:00+05:30',
  premium_source: 'synthetic',
}

describe('API type guards', () => {
  it('accepts the status shape from the spec and rejects an error page', () => {
    expect(isStatus(status)).toBe(true)
    expect(isStatus({ detail: 'not found' })).toBe(false)
    expect(isStatus(null)).toBe(false)
    expect(isStatus('<html></html>')).toBe(false)
  })

  it('accepts a spec-shaped replay step and rejects broken ones', () => {
    expect(isReplayStep(step)).toBe(true)
    expect(isReplayStepArray([step, { ...step, i: 13, action: 'HOLD' }])).toBe(true)
    expect(isReplayStep({ ...step, action: 'DANCE' })).toBe(false)
    expect(isReplayStep({ ...step, narrative: undefined })).toBe(false)
    expect(isReplayStep({ ...step, guard: { allowed: true } })).toBe(false)
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
    expect(isStraddle(straddle)).toBe(true)
    expect(isStraddle({ in_position: true, strike: 'x' })).toBe(false)
  })

  it('checks websocket envelopes', () => {
    expect(isServerEvent({ type: 'observation', at: 'now', data: {} })).toBe(true)
    expect(isServerEvent({ type: 'pong' })).toBe(false)
    expect(isServerEvent('nope')).toBe(false)
  })
})
