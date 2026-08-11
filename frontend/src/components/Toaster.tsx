import { useToastStore } from '@/store/toast'
import { cn } from '@/lib/utils'

export function Toaster() {
  const toasts = useToastStore((s) => s.toasts)
  const dismiss = useToastStore((s) => s.dismiss)

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-100 flex w-full max-w-sm flex-col gap-2">
      {toasts.map((t) => (
        <button
          key={t.id}
          type="button"
          role="status"
          onClick={() => dismiss(t.id)}
          className={cn(
            'rounded-lg border p-4 text-left shadow-lg',
            t.variant === 'destructive'
              ? 'border-destructive bg-destructive text-white'
              : 'border-border bg-card text-card-foreground',
          )}
        >
          <p className="text-sm font-semibold">{t.title}</p>
          {t.description ? <p className="mt-1 text-sm opacity-90">{t.description}</p> : null}
        </button>
      ))}
    </div>
  )
}
