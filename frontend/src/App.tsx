import { Route, Routes } from 'react-router-dom'
import { NavBar } from '@/components/NavBar'
import { Home } from '@/pages/Home'
import { RunsList } from '@/pages/RunsList'
import { RunDetail } from '@/pages/RunDetail'

function NotFound() {
  return <p className="p-6 text-sm text-muted-foreground">Page not found.</p>
}

function App() {
  return (
    <div className="flex min-h-svh flex-col bg-background">
      <NavBar />

      <main className="flex flex-1 flex-col p-6">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/runs" element={<RunsList />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  )
}

export default App
