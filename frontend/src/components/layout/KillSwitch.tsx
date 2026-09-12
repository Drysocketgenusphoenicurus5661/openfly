import { OctagonX } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { useWorkerControls } from '@/api/hooks'
import type { Status } from '@/api/types'
import { ConfirmDialog } from '@/components/common/ConfirmDialog'
import { Button } from '@/components/ui/button'

// Halts everything: square off any open legs, then stop the worker.
export function KillSwitch({ status }: { status: Status | undefined }) {
  const [open, setOpen] = useState(false)
  const { stop, squareOff } = useWorkerControls()
  const busy = stop.isPending || squareOff.isPending
  const idle = !status || status.worker.state === 'stopped'

  const run = async () => {
    try {
      await squareOff.mutateAsync()
    } catch (error) {
      toast.error(`Square off failed: ${error instanceof Error ? error.message : String(error)}`)
    }
    try {
      await stop.mutateAsync()
      toast.success('Kill switch: worker stopped')
    } catch (error) {
      toast.error(`Stop failed: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setOpen(false)
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="border-loss/60 text-loss hover:bg-loss/10 hover:text-loss"
        onClick={() => setOpen(true)}
        disabled={busy}
        title={
          idle
            ? 'Worker is stopped; the kill switch still sends a square-off'
            : 'Square off and stop the worker'
        }
      >
        <OctagonX />
        Kill switch
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Halt everything?"
        destructive
        confirmLabel="Square off and stop"
        loading={busy}
        onConfirm={run}
        description={
          <>
            <p>
              OpenFly will send the exit basket for any open legs (BUY both, at the broker), cancel
              the resting leg stops, and then stop the worker. No new observation, prediction or
              order happens after this until you start the worker again.
            </p>
            <p>
              The OpenAlgo analyzer setting is not changed. Worker state now:{' '}
              <span className="font-medium text-foreground">
                {status?.worker.state ?? 'unknown'}
              </span>
              {status?.worker.mode ? `, ${status.worker.mode}` : ''}.
            </p>
          </>
        }
      />
    </>
  )
}
