// Stands in for the /api/events websocket: advances the simulated day one
// observation every couple of seconds and emits the events the worker would.

import type { ServerEvent } from '@/api/types'
import { mockBus } from './bus'
import { mockDay } from './day'
import { advanceCursor, currentStep, mockState } from './server'

export const MOCK_TICK_MS = 2000

export function startMockEvents(push: (event: ServerEvent) => void): () => void {
  const unsubscribe = mockBus.subscribe(push)
  const emit = (type: string, data: unknown, at: string) => push({ type, at, data })

  emit('worker', { ...mockState.worker }, currentStep().t)
  // Backfill the day so far so the premium line, markers and heatmap start full.
  const steps = mockDay().steps
  for (let i = 0; i < mockState.cursor; i++) {
    emit('observation', steps[i], steps[i].t)
  }
  emit('observation', currentStep(), currentStep().t)
  emit('straddle', mockDay().straddleAt(mockState.cursor), currentStep().t)

  const timer = window.setInterval(() => {
    if (mockState.worker.state !== 'running') return
    const step = advanceCursor()
    emit('observation', step, step.t)
    emit('prediction', { ...step.prediction, stimulus_hash: step.stimulus_hash }, step.t)
    emit('guard', step.guard, step.t)
    emit('straddle', mockDay().straddleAt(step.i), step.t)
    if (step.fills.length > 0) {
      const intents = mockDay()
        .intentsAt(step.i)
        .filter((intent) => intent.created_at === step.t)
      for (const intent of intents) emit('intent', intent, step.t)
      for (const fill of step.fills) emit('order', { ...fill, status: 'complete' }, step.t)
    }
    if (step.action !== 'HOLD' && step.action !== 'NONE') {
      emit(
        'log',
        {
          level: 'info',
          message: `${step.action} at ${step.t.slice(11, 16)}: ${step.narrative.slice(0, 120)}`,
        },
        step.t
      )
    } else if (step.i % 15 === 0) {
      emit(
        'log',
        {
          level: 'debug',
          message: `observation ${step.i}: ${Object.keys(step.rates_hz).length} populations, ${step.compute_seconds.toFixed(2)} s compute`,
        },
        step.t
      )
    }
  }, MOCK_TICK_MS)

  return () => {
    window.clearInterval(timer)
    unsubscribe()
  }
}
