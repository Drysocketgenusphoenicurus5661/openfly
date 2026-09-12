import { lazy, Suspense, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { useBackendStore } from '@/api/backend'
import { Providers } from '@/app/providers'
import { LoadingState } from '@/components/common/EmptyState'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'

const DashboardPage = lazy(() => import('@/pages/Dashboard'))
const BrainPage = lazy(() => import('@/pages/Brain'))
const ReplayPage = lazy(() => import('@/pages/Replay'))
const ExperimentsListPage = lazy(() =>
  import('@/pages/Experiments').then((m) => ({ default: m.ExperimentsListPage }))
)
const ExperimentDetailPage = lazy(() =>
  import('@/pages/Experiments').then((m) => ({ default: m.ExperimentDetailPage }))
)
const OrdersPage = lazy(() => import('@/pages/Orders'))
const SettingsPage = lazy(() => import('@/pages/Settings'))
const SetupPage = lazy(() => import('@/pages/Setup'))

const RETRY_MS = 10_000

function Splash() {
  return (
    <div className="flex h-screen items-center justify-center bg-background text-foreground">
      <div className="text-center">
        <div className="text-lg font-semibold">OpenFly</div>
        <div className="mt-1 text-sm text-muted-foreground">
          Looking for the backend at /api/status
        </div>
      </div>
    </div>
  )
}

function BackendDown() {
  const lastError = useBackendStore((s) => s.lastError)
  const probe = useBackendStore((s) => s.probe)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    const timer = window.setInterval(() => void probe(), RETRY_MS)
    return () => window.clearInterval(timer)
  }, [probe])

  const retry = async () => {
    setRetrying(true)
    try {
      await probe()
    } finally {
      setRetrying(false)
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-background p-6 text-foreground">
      <div className="w-full max-w-md rounded-lg border bg-card p-6 text-card-foreground shadow-sm">
        <div className="text-lg font-semibold">OpenFly backend is not running</div>
        <p className="mt-2 text-sm text-muted-foreground">
          The web UI could not reach GET /api/status on this origin. Start the backend from the
          repository root with:
        </p>
        <pre className="mt-3 rounded-md bg-muted px-3 py-2 font-mono text-sm">uv run app.py</pre>
        <p className="mt-3 text-xs text-muted-foreground">
          In development, run the backend on 127.0.0.1:8000 and this dev server proxies /api to it.
          Retrying automatically every {RETRY_MS / 1000} seconds.
          {lastError ? ` Last error: ${lastError}.` : ''}
        </p>
        <div className="mt-4">
          <Button onClick={retry} disabled={retrying}>
            {retrying ? 'Checking' : 'Retry now'}
          </Button>
        </div>
      </div>
    </div>
  )
}

function page(element: React.ReactNode) {
  return <Suspense fallback={<LoadingState />}>{element}</Suspense>
}

export default function App() {
  const state = useBackendStore((s) => s.state)
  const probe = useBackendStore((s) => s.probe)
  useEffect(() => {
    void probe()
  }, [probe])

  return (
    <Providers>
      {state === 'probing' ? (
        <Splash />
      ) : state === 'down' ? (
        <BackendDown />
      ) : (
        <BrowserRouter>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={page(<DashboardPage />)} />
              <Route path="brain" element={page(<BrainPage />)} />
              <Route path="replay" element={page(<ReplayPage />)} />
              <Route path="experiments" element={page(<ExperimentsListPage />)} />
              <Route path="experiments/:id" element={page(<ExperimentDetailPage />)} />
              <Route path="orders" element={page(<OrdersPage />)} />
              <Route path="settings" element={page(<SettingsPage />)} />
              <Route path="setup" element={page(<SetupPage />)} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      )}
    </Providers>
  )
}
