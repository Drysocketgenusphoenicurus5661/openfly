import { Badge } from '@/components/ui/badge'
import { actionStyle } from '@/lib/actions'
import { cn } from '@/lib/utils'

export function ActionBadge({ action, className }: { action: string; className?: string }) {
  const style = actionStyle(action)
  return (
    <Badge
      variant="outline"
      className={cn('font-medium', style.badgeClass, className)}
      data-action={action}
    >
      {style.label}
    </Badge>
  )
}
