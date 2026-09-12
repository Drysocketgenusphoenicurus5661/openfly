import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import { useModeStore } from './mode'
import type { ExperimentConfig, ReplayRunBody, SettingsPatch, WorkerStartBody } from './types'

export const queryKeys = {
  status: ['status'] as const,
  dataStatus: ['data', 'status'] as const,
  bars: (days: number) => ['market', 'bars', days] as const,
  chain: ['market', 'chain'] as const,
  session: (date?: string) => ['market', 'session', date ?? 'today'] as const,
  circuits: ['brain', 'circuits'] as const,
  brainState: ['brain', 'state'] as const,
  experiments: ['experiments'] as const,
  experiment: (id: string) => ['experiments', id] as const,
  straddle: ['straddle'] as const,
  intents: ['ledger', 'intents'] as const,
  orders: ['orders'] as const,
  positions: ['positions'] as const,
  settings: ['settings'] as const,
  replayDates: ['replay', 'dates'] as const,
  replays: ['replay', 'list'] as const,
  replay: (id: string) => ['replay', id] as const,
}

function useResolved() {
  return useModeStore((s) => s.resolved)
}

export function useStatus(refetchInterval = 5000) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    enabled,
    refetchInterval,
    staleTime: 2000,
  })
}

export function useDataStatus(refetchInterval: number | false = false) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.dataStatus,
    queryFn: api.dataStatus,
    enabled,
    refetchInterval,
  })
}

export function useBars(days = 1) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.bars(days),
    queryFn: () => api.bars({ days }),
    enabled,
    refetchInterval: 30_000,
  })
}

export function useChain() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.chain,
    queryFn: api.chain,
    enabled,
    refetchInterval: 15_000,
  })
}

export function useSession(date?: string) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.session(date),
    queryFn: () => api.session(date),
    enabled,
    staleTime: 60_000,
  })
}

export function useCircuits() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.circuits,
    queryFn: api.circuits,
    enabled,
    staleTime: Infinity,
  })
}

export function useBrainState(refetchInterval: number | false = 5000) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.brainState,
    queryFn: api.brainState,
    enabled,
    refetchInterval,
  })
}

export function useExperiments(refetchInterval: number | false = 5000) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.experiments,
    queryFn: api.experiments,
    enabled,
    refetchInterval,
  })
}

export function useExperiment(id: string | null) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.experiment(id ?? ''),
    queryFn: () => api.experiment(id ?? ''),
    enabled: enabled && !!id,
  })
}

export function useCreateExperiment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (config: ExperimentConfig) => api.createExperiment(config),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.experiments }),
  })
}

export function useStraddle(refetchInterval: number | false = 3000) {
  const enabled = useResolved()
  return useQuery({ queryKey: queryKeys.straddle, queryFn: api.straddle, enabled, refetchInterval })
}

export function useIntents() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.intents,
    queryFn: api.intents,
    enabled,
    refetchInterval: 5000,
  })
}

export function useOrders() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.orders,
    queryFn: api.orders,
    enabled,
    refetchInterval: 5000,
  })
}

export function usePositions() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.positions,
    queryFn: api.positions,
    enabled,
    refetchInterval: 5000,
  })
}

export function useSettings() {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.settings,
    queryFn: api.settings,
    enabled,
    staleTime: 30_000,
  })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (patch: SettingsPatch) => api.updateSettings(patch),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.settings, data)
      qc.invalidateQueries({ queryKey: queryKeys.status })
    },
  })
}

export function useSetAnalyzer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (mode: boolean) => api.setAnalyzer(mode),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.status }),
  })
}

export function useWorkerControls() {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: queryKeys.status })
    qc.invalidateQueries({ queryKey: queryKeys.straddle })
    qc.invalidateQueries({ queryKey: queryKeys.intents })
  }
  const start = useMutation({
    mutationFn: (body: WorkerStartBody) => api.workerStart(body),
    onSuccess: refresh,
  })
  const stop = useMutation({ mutationFn: api.workerStop, onSuccess: refresh })
  const squareOff = useMutation({ mutationFn: api.workerSquareOff, onSuccess: refresh })
  return { start, stop, squareOff }
}

export function useDataPrepare() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: api.dataPrepare,
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.dataStatus }),
  })
}

export function useReplayDates() {
  const enabled = useResolved()
  return useQuery({ queryKey: queryKeys.replayDates, queryFn: api.replayDates, enabled })
}

export function useReplays(refetchInterval: number | false = 5000) {
  const enabled = useResolved()
  return useQuery({ queryKey: queryKeys.replays, queryFn: api.replays, enabled, refetchInterval })
}

export function useReplay(id: string | null) {
  const enabled = useResolved()
  return useQuery({
    queryKey: queryKeys.replay(id ?? ''),
    queryFn: () => api.replay(id ?? ''),
    enabled: enabled && !!id,
    staleTime: 60_000,
  })
}

export function useRunReplay() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: ReplayRunBody) => api.runReplay(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.replays }),
  })
}
