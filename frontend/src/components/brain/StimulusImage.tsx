import { useState } from 'react'
import { cn } from '@/lib/utils'

// The stimulus the fly saw. Pixelated scaling so the bar map stays crisp.
export function StimulusImage({
  src,
  alt = 'Stimulus',
  className,
  cacheKey,
}: {
  src: string
  alt?: string
  className?: string
  cacheKey?: string
}) {
  const [failed, setFailed] = useState(false)
  const url =
    cacheKey && !src.startsWith('data:')
      ? `${src}${src.includes('?') ? '&' : '?'}k=${encodeURIComponent(cacheKey)}`
      : src
  if (failed) {
    return (
      <div
        className={cn(
          'flex aspect-[2/1] w-full items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground',
          className
        )}
      >
        No stimulus image available
      </div>
    )
  }
  return (
    <img
      src={url}
      alt={alt}
      onError={() => setFailed(true)}
      className={cn('w-full rounded-md border bg-black', className)}
      style={{ imageRendering: 'pixelated' }}
    />
  )
}
