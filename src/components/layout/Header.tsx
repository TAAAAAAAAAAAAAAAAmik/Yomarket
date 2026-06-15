import { RefreshCw } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'

interface HeaderProps {
  title: string
  subtitle?: string
}

export function Header({ title, subtitle }: HeaderProps) {
  const queryClient = useQueryClient()

  const handleRefresh = () => {
    queryClient.invalidateQueries()
  }

  return (
    <header className="h-16 border-b border-border bg-bg-secondary px-6 flex items-center justify-between flex-shrink-0">
      <div>
        <h1 className="text-lg font-semibold text-text-primary">{title}</h1>
        {subtitle && <p className="text-xs text-text-secondary">{subtitle}</p>}
      </div>
      <button
        onClick={handleRefresh}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm text-text-secondary hover:text-text-primary hover:bg-bg-elevated transition-colors"
        title="Refresh all data"
      >
        <RefreshCw size={15} />
        <span>Refresh</span>
      </button>
    </header>
  )
}
