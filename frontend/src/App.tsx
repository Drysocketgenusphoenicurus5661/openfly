import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { probeBackend, useModeStore } from '@/api/mode'
import { Providers } from '@/app/providers'
import { LoadingState } from '@/components/common/EmptyState'
import { AppShell } from '@/components/layout/AppShell'

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

export default function App() {
  const resolved = useModeStore((s) => s.resolved)
  useEffect(() => {
    if (!resolved) probeBackend()
  }, [resolved])

  return (
    <Providers>
      {!resolved ? (
        <Splash />
      ) : (
        <BrowserRouter>
          <Routes>
            <Route element={<AppShell />}>
              <Route
                index
                element={
                  <Suspense fallback={<LoadingState />}>
                    <DashboardPage />
                  </Suspense>
                }
              />
              <Route
                path="brain"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <BrainPage />
                  </Suspense>
                }
              />
              <Route
                path="replay"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <ReplayPage />
                  </Suspense>
                }
              />
              <Route
                path="experiments"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <ExperimentsListPage />
                  </Suspense>
                }
              />
              <Route
                path="experiments/:id"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <ExperimentDetailPage />
                  </Suspense>
                }
              />
              <Route
                path="orders"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <OrdersPage />
                  </Suspense>
                }
              />
              <Route
                path="settings"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <SettingsPage />
                  </Suspense>
                }
              />
              <Route
                path="setup"
                element={
                  <Suspense fallback={<LoadingState />}>
                    <SetupPage />
                  </Suspense>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      )}
    </Providers>
  )
}
