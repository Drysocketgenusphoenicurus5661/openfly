// A synthetic trading day: 375 one-minute bars from 09:15 to 15:29, the
// fly's observations, a straddle engine with the same rules the backend
// uses (combined stop, target, lock, per-leg fixed stops, time exit) and
// the ledger those decisions produce.
//
// The "two_straddles" scenario is scripted so that straddle 1 enters at
// 10:20 and hits the combined stop at 11:30, straddle 2 re-enters at 11:45
// at a new strike, locks at 12:30, has its call leg stopped at the broker
// at 13:05 and its put leg reach target at 14:40. The "plain" scenario runs
// the engine with no script so a second date looks different.

import type {
  Action,
  Bar,
  Fill,
  GuardCheck,
  Intent,
  OpenAlgoOrder,
  OpenAlgoPosition,
  RatesHz,
  ReplayStep,
  StepStraddle,
  Straddle,
  StraddleLeg,
} from '@/api/types'
import { istIso } from '@/lib/time'
import { blackPrices, gaussian, hashHex, mulberry32, round, round1, round2 } from './random'
import { barMapStimulus, chartStimulus } from './stimulus'

export const INTERVAL_MIN = 1
export const STEP_COUNT = 375
export const LOT_SIZE = 65
export const FORWARD_BASIS = 65

export interface DayConfig {
  date: string
  expiry: string
  seed: number
  scenario: 'two_straddles' | 'plain'
  encoder: 'A' | 'B' | 'C'
  readout: 'fixed' | 'reservoir' | 'plastic'
  neural_ms: number
  lots: number
  stop_pct: number
  target_pct: number
  lock_after_pct: number
  leg_stop_pct: number
  vix_base: number
  index_open: number
  days_to_expiry_open: number
  tau: number
}

export const DEFAULT_DAY: DayConfig = {
  date: '2026-09-11',
  expiry: '2026-09-15',
  seed: 20260911,
  scenario: 'two_straddles',
  encoder: 'B',
  readout: 'reservoir',
  neural_ms: 200,
  lots: 1,
  stop_pct: 25,
  target_pct: 40,
  lock_after_pct: 15,
  leg_stop_pct: 30,
  vix_base: 12.3,
  index_open: 23385,
  days_to_expiry_open: 3.6,
  tau: 0.1,
}

export interface MockDay {
  config: DayConfig
  bars: Bar[]
  steps: ReplayStep[]
  intents: Intent[]
  orders: OpenAlgoOrder[]
  positions: OpenAlgoPosition[]
  straddleAt: (i: number) => Straddle
  intentsAt: (i: number) => Intent[]
  ordersAt: (i: number) => OpenAlgoOrder[]
  positionsAt: (i: number) => OpenAlgoPosition[]
  pnlAt: (i: number) => number
}

const POPULATIONS = [
  'KC',
  'MBON',
  'DN',
  'DNp20_L',
  'DNp20_R',
  'DNpe017',
  'PAM',
  'PPL1',
  'LC',
  'RANDOM',
]

const BASE_RATES: Record<string, number> = {
  KC: 1.1,
  MBON: 4.2,
  DN: 2.0,
  DNp20_L: 6.0,
  DNp20_R: 7.0,
  DNpe017: 1.5,
  PAM: 2.4,
  PPL1: 1.8,
  LC: 3.1,
  RANDOM: 2.6,
}

function stepTime(date: string, i: number): string {
  const minutes = 9 * 60 + 15 + i * INTERVAL_MIN
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return istIso(date, `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
}

function stepHm(i: number): string {
  const minutes = 9 * 60 + 15 + i * INTERVAL_MIN
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
}

function indexOf(hm: string): number {
  const [h, m] = hm.split(':').map(Number)
  return Math.round((h * 60 + m - (9 * 60 + 15)) / INTERVAL_MIN)
}

// Piecewise linear path through keyframes plus seeded noise.
function pathFromKeyframes(
  keyframes: [string, number][],
  rand: () => number,
  noise: number
): number[] {
  const values: number[] = []
  for (let i = 0; i < STEP_COUNT; i++) {
    let k = 0
    while (k < keyframes.length - 1 && indexOf(keyframes[k + 1][0]) <= i) k++
    const [t0, v0] = keyframes[k]
    const [t1, v1] = keyframes[Math.min(k + 1, keyframes.length - 1)]
    const i0 = indexOf(t0)
    const i1 = indexOf(t1)
    const f = i1 === i0 ? 0 : Math.min(1, Math.max(0, (i - i0) / (i1 - i0)))
    values.push(v0 + (v1 - v0) * f + gaussian(rand) * noise)
  }
  return values
}

const TWO_STRADDLE_INDEX: [string, number][] = [
  ['09:15', 23385],
  ['09:35', 23372],
  ['09:40', 23450],
  ['09:50', 23372],
  ['10:20', 23350],
  ['10:35', 23412],
  ['10:50', 23338],
  ['11:05', 23428],
  ['11:20', 23352],
  ['11:30', 23436],
  ['11:45', 23498],
  ['12:10', 23478],
  ['12:30', 23492],
  ['12:50', 23545],
  ['13:05', 23588],
  ['13:30', 23572],
  ['14:00', 23598],
  ['14:40', 23615],
  ['15:00', 23602],
  ['15:29', 23596],
]

const TWO_STRADDLE_ROI: [string, number][] = [
  ['09:15', 1.08],
  ['09:35', 0.97],
  ['09:39', 0.94],
  ['09:40', 0.86],
  ['09:41', 0.95],
  ['10:15', 0.93],
  ['10:19', 0.92],
  ['10:20', 0.82],
  ['10:45', 0.86],
  ['11:15', 0.98],
  ['11:30', 1.04],
  ['11:44', 0.95],
  ['11:45', 0.84],
  ['12:30', 0.79],
  ['13:05', 0.93],
  ['13:40', 0.88],
  ['14:40', 0.92],
  ['14:49', 0.93],
  ['14:50', 0.87],
  ['14:51', 0.95],
  ['15:00', 0.96],
  ['15:29', 1.0],
]

const TWO_STRADDLE_VIX: [string, number][] = [
  ['09:15', 12.3],
  ['10:20', 12.2],
  ['10:50', 13.3],
  ['11:30', 14.4],
  ['11:45', 13.6],
  ['12:30', 12.6],
  ['15:29', 12.1],
]

const PLAIN_INDEX: [string, number][] = [
  ['09:15', 23410],
  ['09:45', 23388],
  ['10:30', 23402],
  ['11:30', 23420],
  ['12:30', 23396],
  ['13:30', 23384],
  ['14:30', 23405],
  ['15:29', 23412],
]

const PLAIN_ROI: [string, number][] = [
  ['09:15', 1.02],
  ['09:50', 0.91],
  ['10:05', 0.84],
  ['12:00', 0.86],
  ['14:00', 0.95],
  ['15:29', 1.05],
]

function makeBars(date: string, closes: number[], rand: () => number): Bar[] {
  const bars: Bar[] = []
  let prevClose = closes[0] - 4
  for (let i = 0; i < STEP_COUNT; i++) {
    const c = closes[i]
    const o = prevClose
    const wick = 1.5 + Math.abs(gaussian(rand)) * 2.5
    const h = Math.max(o, c) + wick * rand()
    const l = Math.min(o, c) - wick * rand()
    bars.push({
      t: stepTime(date, i),
      o: round1(o),
      h: round1(h),
      l: round1(l),
      c: round1(c),
      v: 0,
    })
    prevClose = c
  }
  return bars
}

interface LegState {
  symbol: string
  kind: 'CE' | 'PE'
  entry_price: number
  ltp: number
  stop_price: number
  stop_order_id: string
  stop_status: 'pending' | 'complete' | 'cancelled' | 'rejected' | 'none'
  status: 'open' | 'stopped' | 'closed'
  exit_price: number | null
}

interface PositionState {
  n: number
  strike: number
  entry_credit: number
  stop_level: number
  target_level: number
  locked: boolean
  legs: LegState[]
  entered_i: number
}

function symbolFor(expiry: string, strike: number, kind: 'CE' | 'PE'): string {
  const [y, m, d] = expiry.split('-')
  const months = [
    'JAN',
    'FEB',
    'MAR',
    'APR',
    'MAY',
    'JUN',
    'JUL',
    'AUG',
    'SEP',
    'OCT',
    'NOV',
    'DEC',
  ]
  return `NIFTY${d}${months[Number(m) - 1]}${y.slice(2)}${strike}${kind}`
}

function orderCost(price: number, qty: number, sell: boolean): number {
  const turnover = price * qty
  const brokerage = Math.min(20, turnover * 0.0003)
  const exchange = turnover * 0.0003503
  const stt = sell ? turnover * 0.001 : 0
  const stamp = sell ? 0 : turnover * 0.00003
  const gst = (brokerage + exchange) * 0.18
  return brokerage + exchange + stt + stamp + gst + turnover * 0.000001
}

export function generateDay(partial: Partial<DayConfig> = {}): MockDay {
  const config: DayConfig = { ...DEFAULT_DAY, ...partial }
  const rand = mulberry32(config.seed)
  const scripted = config.scenario === 'two_straddles'
  const closes = pathFromKeyframes(
    scripted ? TWO_STRADDLE_INDEX : PLAIN_INDEX,
    rand,
    scripted ? 1.2 : 1.5
  ).map((v, i) => (i === 0 ? config.index_open : v))
  const roiPath = pathFromKeyframes(scripted ? TWO_STRADDLE_ROI : PLAIN_ROI, rand, 0.005)
  const vixPath = scripted ? pathFromKeyframes(TWO_STRADDLE_VIX, rand, 0.03) : null
  const bars = makeBars(config.date, closes, rand)
  const qty = LOT_SIZE * config.lots

  const steps: ReplayStep[] = []
  const intents: Intent[] = []
  const orders: OpenAlgoOrder[] = []
  const intentStep: number[] = []
  const orderStep: number[] = []
  const straddleSnapshots: Straddle[] = []
  const pnlSnapshots: number[] = []
  const orderInitial = new Map<string, { status: string; average_price: number; filled: number }>()
  const orderFinalAt = new Map<string, number>()
  const closedAt = new Map<string, number>()

  let position: PositionState | null = null
  let straddles = 0
  let realized = 0
  let costs = 0
  let lastStopI = -100
  let orderSeq = 250900001
  let s2CeLock = 0
  let s2PeLock = 0
  let s2PeStop = 0
  let intentSeq = 1
  const closedSymbols = new Map<string, { entry: number; exit: number }>()

  const script = scripted
    ? {
        enter: [indexOf('10:20'), indexOf('11:45')],
        veto: [indexOf('09:40'), indexOf('14:50')],
        stop: indexOf('11:30'),
        lock: indexOf('12:30'),
        stopLeg: indexOf('13:05'),
        target: indexOf('14:40'),
      }
    : null

  const nextOrderId = () => String(orderSeq++)

  const placeOrder = (
    i: number,
    symbol: string,
    action: 'BUY' | 'SELL',
    pricetype: 'LIMIT' | 'SL-M',
    price: number,
    trigger: number,
    status: string
  ): OpenAlgoOrder => {
    const order: OpenAlgoOrder = {
      orderid: nextOrderId(),
      symbol,
      exchange: 'NFO',
      action,
      quantity: qty,
      price: round(price),
      trigger_price: round(trigger),
      pricetype,
      product: 'NRML',
      order_status: status,
      timestamp: stepTime(config.date, i).replace('+05:30', ''),
      average_price: status === 'complete' ? round(price) : 0,
      filled_quantity: status === 'complete' ? qty : 0,
      strategy: 'openfly',
    }
    orders.push(order)
    orderStep.push(i)
    orderInitial.set(order.orderid, {
      status: order.order_status,
      average_price: Number(order.average_price ?? 0),
      filled: Number(order.filled_quantity ?? 0),
    })
    return order
  }

  const addIntent = (i: number, kind: string, reason: string, legs: Intent['legs']) => {
    intents.push({
      intent_id: `int_${config.date.replace(/-/g, '')}_${String(intentSeq++).padStart(3, '0')}`,
      kind,
      status: 'SETTLED',
      created_at: stepTime(config.date, i),
      reason,
      legs,
    })
    intentStep.push(i)
  }

  const closeLeg = (i: number, leg: LegState, price: number, via: 'stop_order' | 'limit') => {
    leg.exit_price = round(price)
    leg.status = via === 'stop_order' ? 'stopped' : 'closed'
    realized += (leg.entry_price - leg.exit_price) * qty
    costs += orderCost(leg.exit_price, qty, false)
    closedSymbols.set(leg.symbol, { entry: leg.entry_price, exit: leg.exit_price })
    closedAt.set(leg.symbol, i)
    const stop = orders.find((o) => o.orderid === leg.stop_order_id)
    if (via === 'stop_order') {
      leg.stop_status = 'complete'
      if (stop) {
        stop.order_status = 'complete'
        stop.average_price = leg.exit_price
        stop.filled_quantity = qty
        orderFinalAt.set(stop.orderid, i)
      }
    } else {
      leg.stop_status = 'cancelled'
      if (stop) {
        stop.order_status = 'cancelled'
        orderFinalAt.set(stop.orderid, i)
      }
      placeOrder(i, leg.symbol, 'BUY', 'LIMIT', leg.exit_price, 0, 'complete')
    }
  }

  const openStraddle = (
    i: number,
    strike: number,
    ce: number,
    pe: number,
    reentry: boolean
  ): Fill[] => {
    straddles += 1
    const cePrice = round(ce)
    const pePrice = round(pe)
    const legs: LegState[] = (['CE', 'PE'] as const).map((kind) => {
      const entry = kind === 'CE' ? cePrice : pePrice
      const symbol = symbolFor(config.expiry, strike, kind)
      const stopPrice = round(entry * (1 + config.leg_stop_pct / 100))
      placeOrder(i, symbol, 'SELL', 'LIMIT', entry, 0, 'complete')
      const stop = placeOrder(i, symbol, 'BUY', 'SL-M', 0, stopPrice, 'trigger pending')
      costs += orderCost(entry, qty, true)
      return {
        symbol,
        kind,
        entry_price: entry,
        ltp: entry,
        stop_price: stopPrice,
        stop_order_id: stop.orderid,
        stop_status: 'pending',
        status: 'open',
        exit_price: null,
      }
    })
    const credit = round2(cePrice + pePrice)
    position = {
      n: straddles,
      strike,
      entry_credit: credit,
      stop_level: round2(credit * (1 + config.stop_pct / 100)),
      target_level: round2(credit * (1 - config.target_pct / 100)),
      locked: false,
      legs,
      entered_i: i,
    }
    addIntent(
      i,
      'ENTRY',
      `${reentry ? 'readout REENTRY' : 'readout ENTER'} ${roiPath[i].toFixed(2)}`,
      legs.map((leg) => ({
        symbol: leg.symbol,
        side: 'SELL' as const,
        quantity: qty,
        order_id:
          orders.find((o) => o.symbol === leg.symbol && o.action === 'SELL')?.orderid ?? null,
        status: 'complete',
        average_price: leg.entry_price,
      }))
    )
    addIntent(
      i,
      'STOP_LEG',
      `place fixed leg stops at ${config.leg_stop_pct} percent`,
      legs.map((leg) => ({
        symbol: leg.symbol,
        side: 'BUY' as const,
        quantity: qty,
        order_id: leg.stop_order_id,
        status: 'trigger pending',
        average_price: null,
      }))
    )
    return legs.map((leg) => ({
      symbol: leg.symbol,
      side: 'SELL' as const,
      qty,
      price: leg.entry_price,
    }))
  }

  for (let i = 0; i < STEP_COUNT; i++) {
    const hm = stepHm(i)
    const minutes = 9 * 60 + 15 + i * INTERVAL_MIN
    const index = bars[i].c
    const vix = round2(
      vixPath ? vixPath[i] : config.vix_base + Math.sin(i / 9) * 0.35 + gaussian(rand) * 0.08
    )
    const dte = round2(config.days_to_expiry_open - (i / STEP_COUNT) * 0.26)
    const iv = 0.109 + (vix - config.vix_base) * 0.004
    const forward = index + FORWARD_BASIS
    const atm = Math.round(forward / 50) * 50
    let roi = round2(roiPath[i])
    if (script && !position) {
      if (script.enter.includes(i) || script.veto.includes(i)) roi = Math.min(roi, 0.86)
      else roi = Math.max(roi, 0.905)
    }
    const fills: Fill[] = []
    let action: Action = 'NONE'
    const p = position as PositionState | null

    // Leg prices for the straddle that matters at this step.
    const strike = p ? p.strike : atm
    let { ce, pe } = blackPrices(forward, strike, dte, iv)
    ce += gaussian(rand) * 0.15
    pe += gaussian(rand) * 0.15

    // Scripted clamps keep the path honest: nothing fires early, and the
    // scripted events fire exactly where the story says.
    if (script && p) {
      const ceLeg = p.legs[0]
      const peLeg = p.legs[1]
      if (p.n === 1 && i <= script.stop) {
        // Straddle 1 is stopped by a volatility expansion: both legs reprice
        // higher together, so the summed premium reaches the combined stop
        // while each leg stays under its own fixed stop.
        const progress = Math.min(1, (i - p.entered_i) / (script.stop - p.entered_i))
        const g = 1 + 0.235 * progress ** 1.4 + gaussian(rand) * 0.006
        const skew = ((index + FORWARD_BASIS - p.strike) / p.strike) * 6
        ce = ceLeg.entry_price * g * (1 + skew)
        pe = peLeg.entry_price * g * (1 - skew)
        if (i < script.stop) {
          const cap = p.stop_level - 3
          if (ce + pe > cap) {
            const f = cap / (ce + pe)
            ce *= f
            pe *= f
          }
          if (ce > ceLeg.stop_price - 1.5) ce = ceLeg.stop_price - 1.5 - rand()
          if (pe > peLeg.stop_price - 1.5) pe = peLeg.stop_price - 1.5 - rand()
        } else {
          const want = p.stop_level + 1.2
          const f = want / (ce + pe)
          ce *= f
          pe *= f
          if (ce > ceLeg.stop_price - 1) {
            const excess = ce - (ceLeg.stop_price - 1)
            ce -= excess
            pe += excess
          }
          if (pe > peLeg.stop_price - 1) {
            const excess = pe - (peLeg.stop_price - 1)
            pe -= excess
            ce += excess
          }
        }
      } else if (p.n === 2 && i <= script.target) {
        // Straddle 2 follows an explicit path: a smooth decay to the lock,
        // then the call climbs to its fixed stop while the sum stays under
        // the locked stop, then the put drifts down to its target.
        const lockValue = p.entry_credit * (1 - config.lock_after_pct / 100)
        const peTarget = peLeg.entry_price * (1 - config.target_pct / 100)
        if (i <= script.lock) {
          const f = (i - p.entered_i) / Math.max(1, script.lock - p.entered_i)
          const g = 1 - (1 - (lockValue - 1) / p.entry_credit) * f ** 1.1 + gaussian(rand) * 0.004
          const skew = ((index + FORWARD_BASIS - p.strike) / p.strike) * 6
          ce = ceLeg.entry_price * g * (1 + skew)
          pe = peLeg.entry_price * g * (1 - skew)
          if (i < script.lock) {
            const floor = lockValue + 1.5
            if (ce + pe < floor) {
              const f2 = floor / (ce + pe)
              ce *= f2
              pe *= f2
            }
          } else {
            const f2 = (lockValue - 1) / (ce + pe)
            ce *= f2
            pe *= f2
            s2CeLock = ce
            s2PeLock = pe
          }
        } else if (i <= script.stopLeg) {
          const f = (i - script.lock) / Math.max(1, script.stopLeg - script.lock)
          ce = s2CeLock + (ceLeg.stop_price + 0.6 - s2CeLock) * f ** 1.3 + gaussian(rand) * 0.3
          pe = s2PeLock - (s2PeLock - (peTarget + 2.5)) * f + gaussian(rand) * 0.3
          if (i < script.stopLeg) {
            if (ce > ceLeg.stop_price - 1) ce = ceLeg.stop_price - 1 - rand()
            if (ce + pe > p.stop_level - 2) pe = p.stop_level - 2 - ce
            if (pe < peTarget + 2) pe = peTarget + 2
          } else {
            ce = ceLeg.stop_price + 0.6
            pe = Math.min(p.stop_level - 2 - ce, Math.max(peTarget + 2.2, pe))
            s2PeStop = pe
          }
        } else {
          const f = (i - script.stopLeg) / Math.max(1, script.target - script.stopLeg)
          pe = s2PeStop - (s2PeStop - (peTarget - 0.4)) * f ** 0.9 + gaussian(rand) * 0.25
          if (i < script.target) pe = Math.max(pe, peTarget + 0.6)
          else pe = peTarget - 0.4
        }
      }
    }
    ce = round(Math.max(ce, 0.05))
    pe = round(Math.max(pe, 0.05))

    // Rates: a base per population, the fixed decoder's DNp20 asymmetry
    // driven by the readout, and noise.
    const bias = (0.95 - roi) * 12
    const rates_hz: RatesHz = {}
    for (const name of POPULATIONS) {
      let v = BASE_RATES[name] + gaussian(rand) * 0.25
      if (name === 'DNp20_R') v += Math.max(bias, -3)
      if (name === 'DNp20_L') v -= Math.max(bias, -3) * 0.4
      if (name === 'MBON') v += (0.9 - roi) * 4
      if (name === 'KC') v += Math.abs(gaussian(rand)) * 0.3
      rates_hz[name] = round2(Math.max(0, v))
    }
    const left = rates_hz.DNp20_L
    const right = rates_hz.DNp20_R
    const gate = rates_hz.DNpe017 > 1.2 ? 1 : 0
    const difference = round2(right - left)
    const fixedSide =
      gate === 0 ? 'HOLD' : difference > 2 ? 'ENTER' : difference < -2 ? 'EXIT' : 'HOLD'
    const decision = roi < 1 - config.tau ? 'ENTER' : roi > 1 + config.tau ? 'EXIT' : 'HOLD'
    const confidence = round2(Math.min(0.95, Math.max(0.05, Math.abs(1 - roi) * 3 + 0.3)))

    // Guard.
    const inWindow = minutes >= 9 * 60 + 20 && minutes <= 15 * 60 + 15
    const beforeLastEntry = minutes <= 14 * 60 + 30
    const forcedVeto = script ? script.veto.includes(i) : false
    const indexMovePct = forcedVeto ? 0.34 : Math.abs(gaussian(rand)) * 0.05
    const spreadPct = round2(0.12 + Math.abs(gaussian(rand)) * 0.1)
    const quoteAge = round1(0.4 + rand() * 1.5)
    const sinceStop = (i - lastStopI) * INTERVAL_MIN
    const checks: GuardCheck[] = [
      {
        name: 'trade_window',
        ok: inWindow,
        detail: inWindow ? `${hm} within 09:20 to 15:15` : `${hm} outside 09:20 to 15:15`,
      },
      {
        name: 'last_entry',
        ok: beforeLastEntry,
        detail: beforeLastEntry ? `${hm} before last entry 14:30` : `${hm} after last entry 14:30`,
      },
      { name: 'vix_ceiling', ok: vix < 20, detail: `${vix.toFixed(1)} below 20` },
      {
        name: 'spread_pct',
        ok: spreadPct <= 0.5,
        detail: `widest leg spread ${spreadPct.toFixed(2)} percent, limit 0.5`,
      },
      {
        name: 'quote_age',
        ok: quoteAge <= 5,
        detail: `quotes ${quoteAge.toFixed(1)} s old, limit 5 s`,
      },
      {
        name: 'index_move',
        ok: indexMovePct <= 0.3,
        detail: `index moved ${indexMovePct.toFixed(2)} percent since observation, limit 0.3`,
      },
      {
        name: 'daily_loss',
        ok: realized - costs > -10_000,
        detail: `day P&L ${Math.round(realized - costs)} against limit -10000`,
      },
      {
        name: 'max_entries',
        ok: straddles < 10,
        detail: `${straddles} of 10 entries used`,
      },
      {
        name: 'reentry_cooldown',
        ok: sinceStop >= 5,
        detail:
          sinceStop >= 5
            ? 'cooldown 5 minutes satisfied'
            : `${sinceStop} minutes since last stop, need 5`,
      },
    ]
    const allowed = checks.every((c) => c.ok)

    let narrative = ''
    const posNo = p ? p.n : straddles + 1

    if (p) {
      const ceLeg = p.legs[0]
      const peLeg = p.legs[1]
      if (ceLeg.status === 'open') ceLeg.ltp = ce
      if (peLeg.status === 'open') peLeg.ltp = pe
      const openLegs = p.legs.filter((l) => l.status === 'open')
      const combined = round2(openLegs.reduce((s, l) => s + l.ltp, 0))
      const bothOpen = openLegs.length === 2

      const legStopHit = openLegs.find((l) => l.ltp >= l.stop_price)
      const timeExit = minutes >= 15 * 60 + 15
      if (timeExit) {
        action = 'SQUARE_OFF'
        for (const leg of openLegs) {
          closeLeg(i, leg, leg.ltp, 'limit')
          fills.push({ symbol: leg.symbol, side: 'BUY', qty, price: leg.exit_price ?? leg.ltp })
        }
        addIntent(
          i,
          'SQUARE_OFF',
          'time exit 15:15',
          openLegs.map((l) => ({
            symbol: l.symbol,
            side: 'BUY' as const,
            quantity: qty,
            order_id: nextOrderId(),
            status: 'complete',
            average_price: l.exit_price,
          }))
        )
        narrative = `${hm}. Hard exit. Straddle ${p.n} at ${p.strike} bought back at ${combined.toFixed(1)} points, thirty seconds before the 15:15 cutoff. No position is carried past 15:15 on any day.`
        position = null
      } else if (legStopHit) {
        action = 'STOP_LEG'
        closeLeg(i, legStopHit, legStopHit.stop_price + 0.35, 'stop_order')
        fills.push({
          symbol: legStopHit.symbol,
          side: 'BUY',
          qty,
          price: legStopHit.exit_price ?? legStopHit.stop_price,
        })
        const other = p.legs.find((l) => l !== legStopHit && l.status === 'open')
        addIntent(
          i,
          'STOP_LEG',
          `${legStopHit.kind} leg stop ${legStopHit.stop_price.toFixed(2)} executed at broker`,
          [
            {
              symbol: legStopHit.symbol,
              side: 'BUY',
              quantity: qty,
              order_id: legStopHit.stop_order_id,
              status: 'complete',
              average_price: legStopHit.exit_price,
            },
          ]
        )
        if (other) {
          p.stop_level = other.stop_price
          p.target_level = round2(other.entry_price * (1 - config.target_pct / 100))
          narrative = `${hm}. The ${legStopHit.kind === 'CE' ? 'call' : 'put'} leg of straddle ${p.n} hit its fixed stop at ${legStopHit.stop_price.toFixed(1)} (${config.leg_stop_pct} percent above its ${legStopHit.entry_price.toFixed(1)} entry) and the broker's SL-M order filled at ${legStopHit.exit_price?.toFixed(1)}. NIFTY is at ${index.toFixed(0)}, ${Math.round(index - p.strike + FORWARD_BASIS)} points above the ${p.strike} strike on the forward. The ${other.kind === 'CE' ? 'call' : 'put'} keeps running with its own stop at ${other.stop_price.toFixed(1)} and a target at ${p.target_level.toFixed(1)}; on_leg_stop is hold_other.`
        } else {
          position = null
          narrative = `${hm}. Both legs of straddle ${p.n} are now closed.`
        }
      } else if (bothOpen && combined >= p.stop_level) {
        action = 'STOP'
        for (const leg of openLegs) {
          closeLeg(i, leg, leg.ltp, 'limit')
          fills.push({ symbol: leg.symbol, side: 'BUY', qty, price: leg.exit_price ?? leg.ltp })
        }
        addIntent(
          i,
          'EXIT',
          `combined stop ${p.stop_level.toFixed(1)} hit at ${combined.toFixed(1)}`,
          openLegs.map((l) => ({
            symbol: l.symbol,
            side: 'BUY' as const,
            quantity: qty,
            order_id: orders[orders.length - 1].orderid,
            status: 'complete',
            average_price: l.exit_price,
          }))
        )
        lastStopI = i
        const since = bars.slice(p.entered_i, i + 1)
        const lo = Math.min(...since.map((b) => b.l))
        const hi = Math.max(...since.map((b) => b.h))
        narrative = `${hm}. Combined stop. Since entry NIFTY has whipsawed between ${lo.toFixed(0)} and ${hi.toFixed(0)} and INDIAVIX rose from ${steps[p.entered_i].vix.toFixed(1)} to ${vix.toFixed(1)}, so both legs of straddle ${p.n} repriced higher: call ${ceLeg.ltp.toFixed(1)} from ${ceLeg.entry_price.toFixed(1)}, put ${peLeg.ltp.toFixed(1)} from ${peLeg.entry_price.toFixed(1)}. The summed premium reached ${combined.toFixed(1)} against a stop of ${p.stop_level.toFixed(1)} (${config.stop_pct} percent above the ${p.entry_credit.toFixed(1)} credit); neither leg reached its own ${config.leg_stop_pct} percent stop. Both legs were bought back with limit orders and the two SL-M leg stops were cancelled first. Realized ${Math.round(realized - costs)} rupees on the day so far. A re-entry is allowed after a 5 minute cooldown if the readout still says the regime is calm.`
        position = null
      } else if (combined <= p.target_level) {
        action = 'TARGET'
        for (const leg of openLegs) {
          closeLeg(i, leg, leg.ltp, 'limit')
          fills.push({ symbol: leg.symbol, side: 'BUY', qty, price: leg.exit_price ?? leg.ltp })
        }
        addIntent(
          i,
          'EXIT',
          `target ${p.target_level.toFixed(1)} reached at ${combined.toFixed(1)}`,
          openLegs.map((l) => ({
            symbol: l.symbol,
            side: 'BUY' as const,
            quantity: qty,
            order_id: orders[orders.length - 1].orderid,
            status: 'complete',
            average_price: l.exit_price,
          }))
        )
        narrative = bothOpen
          ? `${hm}. Target. The summed premium of straddle ${p.n} decayed to ${combined.toFixed(1)}, ${config.target_pct} percent below the ${p.entry_credit.toFixed(1)} credit. Both legs bought back. Day P&L ${Math.round(realized - costs)} rupees after costs.`
          : `${hm}. Target on the remaining ${openLegs[0].kind === 'CE' ? 'call' : 'put'} leg of straddle ${p.n}: its premium decayed to ${combined.toFixed(1)}, ${config.target_pct} percent below its ${openLegs[0].entry_price.toFixed(1)} entry, and it was bought back with a limit order. The leg stop was cancelled first. Day P&L ${Math.round(realized - costs)} rupees after costs. The last-entry time has passed, so the fly watches but cannot re-enter.`
        position = null
      } else if (
        bothOpen &&
        !p.locked &&
        combined <= p.entry_credit * (1 - config.lock_after_pct / 100)
      ) {
        action = 'LOCK'
        p.locked = true
        p.stop_level = p.entry_credit
        narrative = `${hm}. Lock. The summed premium of straddle ${p.n} has fallen ${config.lock_after_pct} percent from the ${p.entry_credit.toFixed(1)} credit to ${combined.toFixed(1)}, so the combined stop moves down from ${round2(p.entry_credit * (1 + config.stop_pct / 100)).toFixed(1)} to the entry credit. The worst case from here is roughly break-even before costs. The two fixed leg stops stay where they were placed; they are never trailed.`
      } else if (decision === 'EXIT') {
        action = 'EXIT'
        for (const leg of openLegs) {
          closeLeg(i, leg, leg.ltp, 'limit')
          fills.push({ symbol: leg.symbol, side: 'BUY', qty, price: leg.exit_price ?? leg.ltp })
        }
        addIntent(
          i,
          'EXIT',
          `readout EXIT ${roi.toFixed(2)}`,
          openLegs.map((l) => ({
            symbol: l.symbol,
            side: 'BUY' as const,
            quantity: qty,
            order_id: orders[orders.length - 1].orderid,
            status: 'complete',
            average_price: l.exit_price,
          }))
        )
        narrative = `${hm}. Early exit. The readout now expects movement at ${Math.round(roi * 100)} percent of what the premium implies, above the 1 + tau band, so straddle ${p.n} was bought back at ${combined.toFixed(1)} before the mechanical stop.`
        position = null
      } else {
        action = 'HOLD'
        const unrealized = openLegs.reduce((s, l) => s + (l.entry_price - l.ltp) * qty, 0)
        narrative = bothOpen
          ? `${hm}. Holding straddle ${p.n} at ${p.strike}. NIFTY ${index.toFixed(0)}, INDIAVIX ${vix.toFixed(1)}. Summed premium ${combined.toFixed(1)} against a credit of ${p.entry_credit.toFixed(1)}, stop ${p.stop_level.toFixed(1)}${p.locked ? ' (locked at entry)' : ''}, target ${p.target_level.toFixed(1)}. Leg stops at ${ceLeg.stop_price.toFixed(1)} and ${peLeg.stop_price.toFixed(1)} are resting at the broker. The readout puts expected movement at ${Math.round(roi * 100)} percent of implied, still inside the band, so nothing changes. Open P&L ${Math.round(unrealized)} rupees.`
          : `${hm}. Holding the ${openLegs[0].kind === 'CE' ? 'call' : 'put'} leg of straddle ${p.n} alone after the other leg's stop. Premium ${combined.toFixed(1)}, stop ${p.stop_level.toFixed(1)}, target ${p.target_level.toFixed(1)}. NIFTY ${index.toFixed(0)}. Readout ${Math.round(roi * 100)} percent of implied. Open P&L ${Math.round(unrealized)} rupees.`
      }
    } else {
      const wantsEntry = decision === 'ENTER'
      const scriptedEntry = script
        ? script.enter.includes(i)
        : wantsEntry && inWindow && beforeLastEntry
      if (!inWindow) {
        action = 'NONE'
        narrative =
          minutes < 9 * 60 + 20
            ? `${hm}. Observation only. The market opened at ${bars[0].o.toFixed(0)}; the trade window starts at 09:20. The fly watched the first bar (INDIAVIX ${vix.toFixed(1)}) and its readout puts expected movement at ${Math.round(roi * 100)} percent of implied, but no entry is possible yet.`
            : `${hm}. After the 15:15 cutoff. Flat, nothing to do. Day P&L ${Math.round(realized - costs)} rupees after costs.`
      } else if ((wantsEntry || forcedVeto) && !allowed) {
        action = 'VETO'
        const failed = checks.filter((c) => !c.ok)
        narrative = `${hm}. The readout wanted to sell the ${atm} straddle (expected movement ${Math.round(roi * 100)} percent of implied) but the guard said no: ${failed.map((c) => `${c.name.replace(/_/g, ' ')} (${c.detail})`).join('; ')}. The guard only rejects, it never sends. Nothing was done.`
      } else if (scriptedEntry && allowed) {
        const reentry = straddles > 0
        action = reentry ? 'REENTRY' : 'ENTER'
        fills.push(...openStraddle(i, atm, ce, pe, reentry))
        const np = position as unknown as PositionState
        const implied = np.entry_credit
        narrative = `${hm}. ${reentry ? `Re-entry after the earlier stop, cooldown satisfied. ` : ''}The fly watched the last 60 bars of NIFTY (${index > bars[Math.max(0, i - 60)].c ? 'drifting up' : 'drifting down'} ${Math.abs(((index - bars[Math.max(0, i - 60)].c) / index) * 100).toFixed(1)} percent, ${vix < 14 ? 'calm' : 'busy'}), INDIAVIX ${vix.toFixed(1)}. Its readout puts expected movement at ${Math.round(roi * 100)} percent of what the ${atm} straddle is pricing. All ${checks.length} guard checks passed. Sold ${config.lots} lot of the ${atm} straddle (straddle ${np.n} of the day) for ${implied.toFixed(1)} points credit: call ${np.legs[0].entry_price.toFixed(1)}, put ${np.legs[1].entry_price.toFixed(1)}. Combined stop ${np.stop_level.toFixed(1)} (${config.stop_pct} percent above credit), target ${np.target_level.toFixed(1)}, fixed leg stops ${np.legs[0].stop_price.toFixed(1)} and ${np.legs[1].stop_price.toFixed(1)} placed at the broker as SL-M, hard exit 15:15.`
      } else {
        action = 'HOLD'
        narrative =
          wantsEntry && !beforeLastEntry
            ? `${hm}. The readout leans towards selling (${Math.round(roi * 100)} percent of implied) but the last-entry time has passed. Flat into the close.`
            : `${hm}. Flat. NIFTY ${index.toFixed(0)}, INDIAVIX ${vix.toFixed(1)}, the ${atm} straddle offered at ${(ce + pe).toFixed(1)} points. The readout expects movement at ${Math.round(roi * 100)} percent of implied, ${roi >= 1 - config.tau ? 'not below the 1 - tau entry line' : 'inside the hysteresis band'}, so the fly waits.`
      }
    }

    const p2 = position as PositionState | null
    const openLegs = p2 ? p2.legs.filter((l) => l.status === 'open') : []
    const unrealized = openLegs.reduce((s, l) => s + (l.entry_price - l.ltp) * qty, 0)
    const pnlDay = round2(realized + unrealized - costs)
    let combinedNow = p2 ? round2(openLegs.reduce((s, l) => s + l.ltp, 0)) : round2(ce + pe)
    if (!p2 && p) {
      // The straddle closed on this step: quote the ATM straddle from here on.
      const fresh = blackPrices(forward, atm, dte, iv)
      combinedNow = round2(round(fresh.ce) + round(fresh.pe))
    }
    const premium = combinedNow

    const stepStraddle: StepStraddle = p2
      ? {
          in_position: true,
          strike: p2.strike,
          lots: config.lots,
          entry_credit: p2.entry_credit,
          combined_ltp: combinedNow,
          stop_level: p2.stop_level,
          target_level: p2.target_level,
          pnl: round2(unrealized),
          expiry: config.expiry,
          legs: p2.legs.map(toStraddleLeg),
        }
      : {
          in_position: false,
          strike: null,
          lots: null,
          entry_credit: null,
          combined_ltp: null,
          stop_level: null,
          target_level: null,
          pnl: null,
          expiry: null,
          legs: null,
        }

    const stimulusHash = `sha256:${hashHex(`${config.date}:${i}:${index}`)}`
    const stimulusPng =
      config.encoder === 'A'
        ? chartStimulus(bars.slice(0, i + 1))
        : barMapStimulus(`${config.seed}:${i}`)
    const impliedMove = p2 ? p2.entry_credit : round2(ce + pe)
    const top = Object.entries(rates_hz)
      .filter(([name]) => ['DN', 'MBON', 'KC', 'LC', 'PAM'].includes(name))
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3) as [string, number][]

    steps.push({
      i,
      t: stepTime(config.date, i),
      index,
      vix,
      premium,
      days_to_expiry: dte,
      stimulus_hash: stimulusHash,
      stimulus_png: stimulusPng,
      rates_hz,
      fixed_decoder: {
        left_hz: left,
        right_hz: right,
        difference_hz: difference,
        gate_spikes: gate,
        side: fixedSide,
      },
      prediction: {
        realized_over_implied: roi,
        confidence,
        decision,
        tau: config.tau,
      },
      guard: { allowed, checks },
      action,
      straddle: stepStraddle,
      fills,
      pnl_day: pnlDay,
      compute_seconds: round2(0.3 + rand() * 0.4),
      narrative,
      technical: {
        encoder: config.encoder,
        readout: config.readout,
        neural_ms: config.neural_ms,
        features: 3500,
        top_populations: top,
        ridge_alpha: 10.0,
        implied_move_points: impliedMove,
        predicted_move_points: round1(impliedMove * roi),
        sim_ms: (i + 1) * config.neural_ms,
        forward: round1(forward),
        atm_strike: atm,
        costs_so_far: round2(costs),
        straddle_no: p2 ? p2.n : posNo - 1,
      },
    })

    straddleSnapshots.push(toStraddle(p2, config, i, combinedNow, unrealized))
    pnlSnapshots.push(pnlDay)
  }

  const positions: OpenAlgoPosition[] = []
  const positionsAt = (i: number): OpenAlgoPosition[] => {
    const seen = new Map<string, OpenAlgoPosition>()
    for (let k = 0; k <= i; k++) {
      const legs = steps[k].straddle.legs ?? []
      for (const leg of legs) {
        const closeStep = closedAt.get(leg.symbol)
        const closed =
          closeStep !== undefined && closeStep <= i ? closedSymbols.get(leg.symbol) : undefined
        seen.set(leg.symbol, {
          symbol: leg.symbol,
          exchange: 'NFO',
          product: 'NRML',
          quantity: closed ? 0 : -qty,
          average_price: leg.entry_price,
          ltp: closed ? closed.exit : leg.ltp,
          pnl: closed
            ? round2((closed.entry - closed.exit) * qty)
            : round2((leg.entry_price - leg.ltp) * qty),
        })
      }
    }
    return [...seen.values()]
  }
  positions.push(...positionsAt(STEP_COUNT - 1))
  const ordersAt = (i: number): OpenAlgoOrder[] =>
    orders
      .filter((_, k) => orderStep[k] <= i)
      .map((o) => {
        const finalAt = orderFinalAt.get(o.orderid)
        if (finalAt !== undefined && finalAt > i) {
          const initial = orderInitial.get(o.orderid)
          return {
            ...o,
            order_status: initial?.status ?? o.order_status,
            average_price: initial?.average_price ?? 0,
            filled_quantity: initial?.filled ?? 0,
          }
        }
        return o
      })

  return {
    config,
    bars,
    steps,
    intents,
    orders,
    positions,
    straddleAt: (i) => straddleSnapshots[Math.max(0, Math.min(i, STEP_COUNT - 1))],
    intentsAt: (i) => intents.filter((_, k) => intentStep[k] <= i),
    ordersAt,
    positionsAt,
    pnlAt: (i) => pnlSnapshots[Math.max(0, Math.min(i, STEP_COUNT - 1))],
  }
}

function toStraddleLeg(leg: LegState): StraddleLeg {
  return {
    symbol: leg.symbol,
    side: 'SELL',
    qty: LOT_SIZE,
    entry_price: leg.entry_price,
    ltp: leg.status === 'open' ? leg.ltp : (leg.exit_price ?? leg.ltp),
    stop_price: leg.stop_price,
    stop_order_id: leg.stop_order_id,
    stop_status: leg.stop_status,
    status: leg.status,
  }
}

function toStraddle(
  p: PositionState | null,
  config: DayConfig,
  i: number,
  combined: number,
  unrealized: number
): Straddle {
  if (!p) {
    return {
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
  }
  void i
  return {
    in_position: true,
    expiry: config.expiry,
    strike: p.strike,
    lots: config.lots,
    legs: p.legs.map(toStraddleLeg),
    entry_credit: p.entry_credit,
    combined_ltp: combined,
    stop_level: p.stop_level,
    target_level: p.target_level,
    pnl: round2(unrealized),
    entered_at: stepTime(config.date, p.entered_i).replace(':00+05:30', ':04+05:30'),
    square_off_at: istIso(config.date, '15:15'),
  }
}

let cached: MockDay | null = null
export function mockDay(): MockDay {
  if (!cached) cached = generateDay()
  return cached
}

const otherDays = new Map<string, MockDay>()
export function mockDayFor(date: string, overrides: Partial<DayConfig> = {}): MockDay {
  if (date === DEFAULT_DAY.date && Object.keys(overrides).length === 0) return mockDay()
  const key = `${date}:${JSON.stringify(overrides)}`
  let day = otherDays.get(key)
  if (!day) {
    const seed = Number(date.replace(/-/g, '')) + (overrides.neural_ms ?? 0)
    day = generateDay({
      date,
      seed,
      scenario: 'plain',
      index_open: 23300 + (seed % 200),
      ...overrides,
    })
    otherDays.set(key, day)
  }
  return day
}
