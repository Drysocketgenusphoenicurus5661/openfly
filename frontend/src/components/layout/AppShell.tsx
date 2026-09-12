import {
  Brain,
  FlaskConical,
  LayoutDashboard,
  ListOrdered,
  Moon,
  PlayCircle,
  Plug,
  Settings,
  Sun,
} from 'lucide-react'
import { useTheme } from 'next-themes'
import { NavLink, Outlet } from 'react-router'
import { useEvents } from '@/api/events'
import { useChain, useSettings, useStatus } from '@/api/hooks'
import { Button } from '@/components/ui/button'
import { describeExpiry } from '@/lib/time'
import { cn } from '@/lib/utils'
import { KillSwitch } from './KillSwitch'
import { ModeBadge } from './ModeBadge'
import { SessionClock } from './SessionClock'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/brain', label: 'Brain', icon: Brain },
  { to: '/replay', label: 'Replay', icon: PlayCircle },
  { to: '/experiments', label: 'Experiments', icon: FlaskConical },
  { to: '/orders', label: 'Orders', icon: ListOrdered },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/setup', label: 'Setup', icon: Plug },
]

function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme()
  const dark = resolvedTheme !== 'light'
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      title={dark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {dark ? <Sun /> : <Moon />}
    </Button>
  )
}

export function AppShell() {
  const { data: status } = useStatus()
  const { data: chain } = useChain()
  const { data: settings } = useSettings()
  const { status: socket } = useEvents()
  const selection = chain?.expiry_selection ?? settings?.strategy.expiry_selection ?? null
  const expiryLabel = chain
    ? describeExpiry(chain.expiry, selection)
    : selection
      ? `${selection}, chain not loaded`
      : null

  return (
    <div className="flex h-screen min-w-[1280px] overflow-hidden bg-background text-foreground">
      <aside className="flex w-52 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
        <div className="flex h-14 items-center gap-2 border-b px-4">
          <span className="text-base font-semibold tracking-tight">OpenFly</span>
          <span className="text-[10px] text-muted-foreground">{status?.version ?? ''}</span>
        </div>
        <nav className="flex-1 space-y-0.5 p-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors',
                  isActive
                    ? 'bg-sidebar-accent font-medium text-sidebar-accent-foreground'
                    : 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground'
                )
              }
            >
              <item.icon className="size-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-1 border-t p-3 text-[11px] text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                'inline-block size-2 rounded-full',
                socket === 'open' ? 'bg-profit' : socket === 'connecting' ? 'bg-amber' : 'bg-loss'
              )}
            />
            events {socket}
          </div>
          <div>
            OpenAlgo{' '}
            {status?.openalgo.reachable
              ? `reachable (${status.openalgo.broker ?? 'broker'})`
              : 'unreachable'}
          </div>
          <div>analyzer {status?.openalgo.analyzer_mode ? 'on' : 'off'}</div>
        </div>
        <a
          href="https://marketcalls.github.io/openalgo-charts/"
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-2 border-t px-3 py-2.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          title="OpenAlgo Charts documentation"
        >
          <span
            aria-hidden="true"
            className="inline-block size-6 shrink-0 bg-current"
            style={{
              maskImage: 'url(/openalgo-mark.svg)',
              WebkitMaskImage: 'url(/openalgo-mark.svg)',
              maskSize: 'contain',
              WebkitMaskSize: 'contain',
              maskRepeat: 'no-repeat',
              WebkitMaskRepeat: 'no-repeat',
              maskPosition: 'center',
              WebkitMaskPosition: 'center',
            }}
          />
          <span>Charts by OpenAlgo Charts</span>
        </a>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-4 border-b px-4">
          <ModeBadge status={status} />
          <SessionClock session={status?.session} compact className="w-[420px]" />
          {expiryLabel && (
            <span
              className="rounded-md border px-2 py-1 text-[11px] text-muted-foreground"
              title="Current expiry the strategy trades"
            >
              Expiry <span className="tabular font-medium text-foreground">{expiryLabel}</span>
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <KillSwitch status={status} />
          </div>
        </header>
        <main className="min-w-0 flex-1 overflow-y-auto p-4">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
