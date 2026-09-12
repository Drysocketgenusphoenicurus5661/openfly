import { beforeEach, describe, expect, it } from 'vitest'
import { stepIntervalMs, useReplayPlayer } from './replayPlayer'

describe('replay player store', () => {
  beforeEach(() => {
    useReplayPlayer.getState().reset()
  })

  it('loads a replay and clamps navigation to the step range', () => {
    const s = useReplayPlayer.getState()
    s.load('rp_1', 375)
    expect(useReplayPlayer.getState().index).toBe(0)
    s.prev()
    expect(useReplayPlayer.getState().index).toBe(0)
    s.next()
    s.next()
    expect(useReplayPlayer.getState().index).toBe(2)
    s.setIndex(1000)
    expect(useReplayPlayer.getState().index).toBe(374)
    s.setIndex(-5)
    expect(useReplayPlayer.getState().index).toBe(0)
    s.last()
    expect(useReplayPlayer.getState().index).toBe(374)
    s.first()
    expect(useReplayPlayer.getState().index).toBe(0)
  })

  it('ticks while playing and pauses at the end', () => {
    const s = useReplayPlayer.getState()
    s.load('rp_1', 3)
    s.tick()
    expect(useReplayPlayer.getState().index).toBe(0)
    s.play()
    expect(useReplayPlayer.getState().playing).toBe(true)
    s.tick()
    s.tick()
    expect(useReplayPlayer.getState().index).toBe(2)
    s.tick()
    expect(useReplayPlayer.getState().playing).toBe(false)
    expect(useReplayPlayer.getState().index).toBe(2)
    // Playing again from the end starts over.
    s.play()
    expect(useReplayPlayer.getState().index).toBe(0)
    s.toggle()
    expect(useReplayPlayer.getState().playing).toBe(false)
  })

  it('maps speed to steps per second', () => {
    const s = useReplayPlayer.getState()
    expect(useReplayPlayer.getState().speed).toBe(5)
    s.setSpeed(20)
    expect(useReplayPlayer.getState().speed).toBe(20)
    expect(stepIntervalMs(1)).toBe(1000)
    expect(stepIntervalMs(5)).toBe(200)
    expect(stepIntervalMs(20)).toBe(50)
  })

  it('does not play an empty replay', () => {
    const s = useReplayPlayer.getState()
    s.load(null, 0)
    s.play()
    expect(useReplayPlayer.getState().playing).toBe(false)
  })
})
