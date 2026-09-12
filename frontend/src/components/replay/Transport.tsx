import { ChevronsLeft, ChevronsRight, Pause, Play, SkipBack, SkipForward } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Slider } from '@/components/ui/slider'
import { useReplayClock } from '@/hooks/useReplayClock'
import { formatTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { SPEEDS, type Speed, useReplayPlayer } from '@/stores/replayPlayer'

export function Transport({ times }: { times: string[] }) {
  useReplayClock()
  const index = useReplayPlayer((s) => s.index)
  const total = useReplayPlayer((s) => s.total)
  const playing = useReplayPlayer((s) => s.playing)
  const speed = useReplayPlayer((s) => s.speed)
  const { toggle, next, prev, first, last, setIndex, setSpeed } = useReplayPlayer.getState()

  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center gap-1">
        <Button
          variant="outline"
          size="icon-sm"
          onClick={first}
          title="First step"
          disabled={total === 0}
        >
          <ChevronsLeft />
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={prev}
          title="Step back"
          disabled={total === 0}
        >
          <SkipBack />
        </Button>
        <Button
          variant="default"
          size="icon-sm"
          onClick={toggle}
          title={playing ? 'Pause' : 'Play'}
          disabled={total === 0}
        >
          {playing ? <Pause /> : <Play />}
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={next}
          title="Step forward"
          disabled={total === 0}
        >
          <SkipForward />
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={last}
          title="Last step"
          disabled={total === 0}
        >
          <ChevronsRight />
        </Button>
      </div>
      <div className="flex items-center rounded-md border p-0.5">
        {SPEEDS.map((s) => (
          <button
            type="button"
            key={s}
            onClick={() => setSpeed(s as Speed)}
            className={cn(
              'rounded px-2 py-0.5 text-xs',
              speed === s
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground'
            )}
          >
            {s}x
          </button>
        ))}
      </div>
      <Slider
        className="flex-1"
        min={0}
        max={Math.max(0, total - 1)}
        step={1}
        value={[index]}
        onValueChange={(value) => setIndex(value[0] ?? 0)}
        aria-label="Replay scrubber"
      />
      <span className="tabular w-36 text-right text-xs text-muted-foreground">
        {total > 0 ? `${formatTime(times[index])} (${index + 1} of ${total})` : 'no steps'}
      </span>
    </div>
  )
}
