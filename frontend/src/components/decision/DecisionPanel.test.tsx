import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ReplayStep } from '@/api/types'
import { DecisionPanel } from './DecisionPanel'

const step: ReplayStep = {
  i: 65,
  t: '2026-09-11T10:20:00+05:30',
  index: 23350.2,
  vix: 12.1,
  premium: 201.3,
  days_to_expiry: 3.6,
  stimulus_hash: 'sha256:abcdef0123456789',
  stimulus_png: '/api/replay/rp_x/stimulus/65.png',
  rates_hz: { KC: 1.2, MBON: 4.5, DN: 2.1, DNp20_L: 6.0, DNp20_R: 8.0, DNpe017: 2.0 },
  fixed_decoder: { left_hz: 6.0, right_hz: 8.0, difference_hz: 2.0, gate_spikes: 1, side: 'ENTER' },
  prediction: { realized_over_implied: 0.82, confidence: 0.61, decision: 'ENTER', tau: 0.1 },
  guard: {
    allowed: false,
    checks: [
      { name: 'trade_window', ok: true, detail: '10:20 within 09:20 to 14:30' },
      { name: 'vix_ceiling', ok: true, detail: '12.1 below 20' },
      {
        name: 'index_move',
        ok: false,
        detail: 'index moved 0.34 percent since observation, limit 0.3',
      },
    ],
  },
  action: 'VETO',
  straddle: {
    in_position: false,
    strike: null,
    lots: null,
    entry_credit: null,
    combined_ltp: null,
    stop_level: null,
    target_level: null,
    pnl: null,
  },
  fills: [],
  pnl_day: -120.5,
  compute_seconds: 0.4,
  narrative: 'The readout wanted to sell the 23350 straddle but the guard said no: index move.',
  technical: {
    encoder: 'B',
    readout: 'reservoir',
    neural_ms: 200,
    features: 3500,
    top_populations: [
      ['DN', 2.1],
      ['MBON', 4.5],
    ],
    ridge_alpha: 10.0,
    implied_move_points: 201.3,
    predicted_move_points: 165.1,
  },
}

describe('DecisionPanel', () => {
  it('shows the narrative and the summary row', () => {
    render(<DecisionPanel step={step} straddleNo={1} />)
    expect(screen.getByTestId('narrative')).toHaveTextContent('the guard said no')
    expect(screen.getByText('Straddle 1')).toBeInTheDocument()
    expect(screen.getAllByText('Veto').length).toBeGreaterThan(0)
    expect(screen.getByText('0.82 of implied')).toBeInTheDocument()
    expect(screen.getByText('10:20', { selector: 'span' })).toBeInTheDocument()
  })

  it('shows guard checks with pass and fail marks on the technical tab', async () => {
    const user = userEvent.setup()
    render(<DecisionPanel step={step} />)
    await user.click(screen.getByRole('tab', { name: 'Technical' }))
    const list = screen.getByTestId('guard-checklist')
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('pass')
    expect(items[0]).toHaveTextContent('trade window')
    expect(items[2]).toHaveTextContent('fail')
    expect(items[2]).toHaveTextContent('index moved 0.34 percent')
    expect(screen.getByText('rejected, 2 of 3 checks passed')).toBeInTheDocument()
    expect(screen.getByText('reservoir')).toBeInTheDocument()
    expect(screen.getByText('DN 2.1, MBON 4.5')).toBeInTheDocument()
    expect(screen.getByText('0.90 to 1.10')).toBeInTheDocument()
    expect(screen.getByTitle('sha256:abcdef0123456789')).toBeInTheDocument()
  })
})
