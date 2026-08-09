import { useMutation } from '@tanstack/react-query'
import { Activity, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

type HealthResponse = {
  status: string
  db: boolean
  version: string
}

async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch('/api/health')
  if (!res.ok) {
    throw new Error(`Request failed with status ${res.status}`)
  }
  return res.json()
}

function App() {
  const { mutate, data, error, isPending } = useMutation({
    mutationFn: fetchHealth,
  })

  return (
    <div className="flex min-h-svh flex-col bg-background">
      <header className="flex h-[72px] shrink-0 items-center border-b px-6">
        <h1 className="text-lg font-bold text-foreground">DocAnnotate</h1>
      </header>

      <main className="flex flex-1 items-center justify-center p-8">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>Backend status</CardTitle>
            <CardDescription>
              Ping the FastAPI service and confirm the database connection is
              healthy.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {data ? (
              <pre className="overflow-x-auto rounded-lg bg-muted p-4 text-sm text-muted-foreground">
                {JSON.stringify(data, null, 2)}
              </pre>
            ) : error ? (
              <p className="text-sm text-destructive">{error.message}</p>
            ) : (
              <p className="text-sm text-muted-foreground">
                No response yet. Click the button below to check.
              </p>
            )}
          </CardContent>
          <CardFooter>
            <Button onClick={() => mutate()} disabled={isPending}>
              {isPending ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Activity />
              )}
              Check backend
            </Button>
          </CardFooter>
        </Card>
      </main>
    </div>
  )
}

export default App
