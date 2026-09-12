import { cn } from '@/lib/utils'

function render(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number')
    return Number.isInteger(value)
      ? String(value)
      : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    if (value.every((v) => Array.isArray(v) && v.length === 2)) {
      return value.map((v) => `${(v as unknown[])[0]} ${render((v as unknown[])[1])}`).join(', ')
    }
    return value.map(render).join(', ')
  }
  return JSON.stringify(value)
}

export function KeyValueGrid({
  data,
  className,
  order,
}: {
  data: Record<string, unknown>
  className?: string
  order?: string[]
}) {
  const keys = order
    ? [...order.filter((k) => k in data), ...Object.keys(data).filter((k) => !order.includes(k))]
    : Object.keys(data)
  return (
    <dl className={cn('grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm', className)}>
      {keys.map((key) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{key.replace(/_/g, ' ')}</dt>
          <dd className="tabular truncate font-medium" title={render(data[key])}>
            {render(data[key])}
          </dd>
        </div>
      ))}
    </dl>
  )
}
