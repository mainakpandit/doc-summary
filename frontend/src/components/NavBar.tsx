import { NavLink } from 'react-router-dom'
import { Input } from '@/components/ui/input'
import { useReviewerStore } from '@/store/reviewer'
import { cn } from '@/lib/utils'

const LINKS = [
  { to: '/', label: 'Home', end: true },
  { to: '/register', label: 'Register', end: false },
  { to: '/runs', label: 'Runs', end: false },
]

export function NavBar() {
  const reviewer = useReviewerStore((s) => s.reviewer)
  const setReviewer = useReviewerStore((s) => s.setReviewer)

  return (
    <header className="flex h-[72px] shrink-0 items-center gap-8 border-b px-6">
      <span className="text-lg font-bold text-foreground">DocAnnotate</span>
      <nav className="flex items-center gap-4">
        {LINKS.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            end={link.end}
            className={({ isActive }) =>
              cn(
                'text-sm font-medium transition-colors',
                isActive
                  ? 'text-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )
            }
          >
            {link.label}
          </NavLink>
        ))}
      </nav>

      <div className="ml-auto flex items-center gap-2">
        <label htmlFor="reviewer-identity" className="text-sm text-muted-foreground">
          X-Reviewer
        </label>
        <Input
          id="reviewer-identity"
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="your name"
          className="h-8 w-40"
        />
      </div>
    </header>
  )
}
