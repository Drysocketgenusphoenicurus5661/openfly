import { cn } from '@/lib/utils'

export function EmptyState({ text, className }: { text: string; className?: string }) {
  return (
    <div
      className={cn(
        'flex items-center justify-center rounded-md border border-dashed p-6 text-sm text-muted-foreground',
        className
      )}
    >
      {text}
    </div>
  )
}

export function LoadingState({
  text = 'Loading',
  className,
}: {
  text?: string
  className?: string
}) {
  return <div className={cn('p-6 text-sm text-muted-foreground', className)}>{text}</div>
}

export function ErrorState({ error, className }: { error: unknown; className?: string }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div
      className={cn('rounded-md border border-loss/40 bg-loss/10 p-3 text-sm text-loss', className)}
    >
      {message}
    </div>
  )
}
