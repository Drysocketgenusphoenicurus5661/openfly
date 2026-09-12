import { describe, expect, it } from 'vitest'
import { describeStopBasis } from './stops'

describe('describeStopBasis', () => {
  it('describes adaptive stops in trader language', () => {
    expect(
      describeStopBasis({
        mode: 'adaptive',
        horizon_minutes: 60,
        expected_move_points: 95.0,
        implied_move_points: 88.0,
        realized_move_points: 95.0,
        leg_stop_pct: { ce: 31.2, pe: 28.7 },
        combined_stop_pct: 18.4,
      })
    ).toBe(
      'Stops sized for a 95 point move over 60 minutes (implied 88, realized 95): call 31 percent, put 29 percent, combined 18 percent.'
    )
  })

  it('describes fixed stops', () => {
    expect(
      describeStopBasis({
        mode: 'fixed',
        horizon_minutes: 60,
        expected_move_points: 0,
        implied_move_points: 0,
        realized_move_points: 0,
        leg_stop_pct: { ce: 30, pe: 30 },
        combined_stop_pct: 25,
      })
    ).toBe('Fixed stops: call 30 percent, put 30 percent, combined 25 percent.')
  })
})
