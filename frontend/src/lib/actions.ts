import type { Action, ReplayStep } from '@/api/types'

// One colour per action, used by badges, timeline markers and chart markers.
// ENTER green, EXIT and TARGET blue, STOP and STOP_LEG red, SQUARE_OFF
// orange, VETO grey, HOLD muted, LOCK violet, REENTRY green like ENTER.
export interface ActionStyle {
  label: string
  cssVar: string
  badgeClass: string
  textClass: string
  hex: { light: string; dark: string }
  marker: boolean
}

export const ACTION_STYLES: Record<Action, ActionStyle> = {
  ENTER: {
    label: 'Enter',
    cssVar: 'var(--action-enter)',
    badgeClass: 'bg-action-enter/15 text-action-enter border-action-enter/40',
    textClass: 'text-action-enter',
    hex: { light: '#15803d', dark: '#4ade80' },
    marker: true,
  },
  REENTRY: {
    label: 'Re-entry',
    cssVar: 'var(--action-enter)',
    badgeClass: 'bg-action-enter/15 text-action-enter border-action-enter/40',
    textClass: 'text-action-enter',
    hex: { light: '#15803d', dark: '#4ade80' },
    marker: true,
  },
  EXIT: {
    label: 'Exit',
    cssVar: 'var(--action-exit)',
    badgeClass: 'bg-action-exit/15 text-action-exit border-action-exit/40',
    textClass: 'text-action-exit',
    hex: { light: '#2563eb', dark: '#60a5fa' },
    marker: true,
  },
  TARGET: {
    label: 'Target',
    cssVar: 'var(--action-exit)',
    badgeClass: 'bg-action-exit/15 text-action-exit border-action-exit/40',
    textClass: 'text-action-exit',
    hex: { light: '#2563eb', dark: '#60a5fa' },
    marker: true,
  },
  STOP: {
    label: 'Stop',
    cssVar: 'var(--action-stop)',
    badgeClass: 'bg-action-stop/15 text-action-stop border-action-stop/40',
    textClass: 'text-action-stop',
    hex: { light: '#dc2626', dark: '#f87171' },
    marker: true,
  },
  STOP_LEG: {
    label: 'Leg stop',
    cssVar: 'var(--action-stop)',
    badgeClass: 'bg-action-stop/15 text-action-stop border-action-stop/40',
    textClass: 'text-action-stop',
    hex: { light: '#dc2626', dark: '#f87171' },
    marker: true,
  },
  LOCK: {
    label: 'Lock',
    cssVar: 'var(--action-lock)',
    badgeClass: 'bg-action-lock/15 text-action-lock border-action-lock/40',
    textClass: 'text-action-lock',
    hex: { light: '#7c3aed', dark: '#c084fc' },
    marker: true,
  },
  SQUARE_OFF: {
    label: 'Square off',
    cssVar: 'var(--action-square-off)',
    badgeClass: 'bg-action-square-off/15 text-action-square-off border-action-square-off/40',
    textClass: 'text-action-square-off',
    hex: { light: '#ea580c', dark: '#fb923c' },
    marker: true,
  },
  VETO: {
    label: 'Veto',
    cssVar: 'var(--action-veto)',
    badgeClass: 'bg-action-veto/15 text-action-veto border-action-veto/40',
    textClass: 'text-action-veto',
    hex: { light: '#6b7280', dark: '#9ca3af' },
    marker: true,
  },
  HOLD: {
    label: 'Hold',
    cssVar: 'var(--action-hold)',
    badgeClass: 'bg-muted text-muted-foreground border-transparent',
    textClass: 'text-muted-foreground',
    hex: { light: '#a3a3a3', dark: '#525252' },
    marker: false,
  },
  NONE: {
    label: 'None',
    cssVar: 'var(--action-hold)',
    badgeClass: 'bg-muted text-muted-foreground border-transparent',
    textClass: 'text-muted-foreground',
    hex: { light: '#a3a3a3', dark: '#525252' },
    marker: false,
  },
}

export function actionStyle(action: string): ActionStyle {
  return ACTION_STYLES[action as Action] ?? ACTION_STYLES.NONE
}

export function actionHex(action: string, dark: boolean): string {
  const style = actionStyle(action)
  return dark ? style.hex.dark : style.hex.light
}

export const ENTRY_ACTIONS: Action[] = ['ENTER', 'REENTRY']
export const EXIT_ACTIONS: Action[] = ['EXIT', 'STOP', 'TARGET', 'SQUARE_OFF']

// Numbers the straddles of a day: every ENTER or REENTRY starts a new one.
// Returns, per step, the straddle number it belongs to (0 when flat) and
// the strike, plus a list of spans for timelines.
export interface StraddleSpan {
  n: number
  strike: number | null
  from: number
  to: number
  entryAction: Action
  exitAction: Action | null
}

export function numberStraddles(steps: ReplayStep[]): {
  perStep: { n: number; strike: number | null }[]
  spans: StraddleSpan[]
} {
  const perStep: { n: number; strike: number | null }[] = []
  const spans: StraddleSpan[] = []
  let n = 0
  let open: StraddleSpan | null = null
  for (const step of steps) {
    if (ENTRY_ACTIONS.includes(step.action)) {
      n += 1
      open = {
        n,
        strike: step.straddle.strike,
        from: step.i,
        to: step.i,
        entryAction: step.action,
        exitAction: null,
      }
      spans.push(open)
    }
    if (open) {
      open.to = step.i
      if (open.strike === null && step.straddle.strike !== null) open.strike = step.straddle.strike
    }
    perStep.push(
      open && step.straddle.in_position
        ? { n: open.n, strike: open.strike }
        : { n: 0, strike: null }
    )
    if (open && !step.straddle.in_position && !ENTRY_ACTIONS.includes(step.action)) {
      open.exitAction = step.action
      open = null
    }
  }
  return { perStep, spans }
}
