import type { StopBasis } from '@/api/types'
import { fmtNum } from './format'

// One sentence for traders about how the current straddle's stops were sized.
export function describeStopBasis(basis: StopBasis): string {
  const legs = `call ${fmtNum(basis.leg_stop_pct.ce, 0)} percent, put ${fmtNum(basis.leg_stop_pct.pe, 0)} percent, combined ${fmtNum(basis.combined_stop_pct, 0)} percent`
  if (basis.mode === 'fixed') return `Fixed stops: ${legs}.`
  return `Stops sized for a ${fmtNum(basis.expected_move_points, 0)} point move over ${basis.horizon_minutes} minutes (implied ${fmtNum(basis.implied_move_points, 0)}, realized ${fmtNum(basis.realized_move_points, 0)}): ${legs}.`
}
