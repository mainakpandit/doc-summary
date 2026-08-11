import { NavLink } from 'react-router-dom'
import { cn } from '@/lib/utils'

const LINKS = [
  { to: '/', label: 'Home', end: true },
  { to: '/runs', label: 'Runs', end: false },
]

export function NavBar() {
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
    </header>
  )
}
