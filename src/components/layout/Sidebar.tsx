import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Package,
  MessageSquare,
  Settings,
  Store,
} from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/products', label: 'Ads', icon: Package, end: false },
  { to: '/chats', label: 'Chats', icon: MessageSquare, end: false },
  { to: '/settings', label: 'Settings', icon: Settings, end: false },
]

export function Sidebar() {
  const shopName = useAuthStore((s) => s.shopName)

  return (
    <aside className="w-60 min-h-screen bg-bg-secondary border-r border-border flex flex-col">
      {/* Logo / brand */}
      <div className="flex items-center gap-3 px-5 py-5 border-b border-border">
        <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center flex-shrink-0">
          <Store size={16} className="text-white" />
        </div>
        <div className="overflow-hidden">
          <p className="text-xs text-text-secondary leading-none mb-0.5">YooMarket</p>
          <p className="text-sm font-semibold text-text-primary leading-none truncate">{shopName || 'My Shop'}</p>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-accent/15 text-accent'
                  : 'text-text-secondary hover:bg-bg-elevated hover:text-text-primary'
              }`
            }
          >
            <Icon size={18} />
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="px-5 py-4 border-t border-border">
        <p className="text-xs text-text-muted">Integration v1</p>
      </div>
    </aside>
  )
}
