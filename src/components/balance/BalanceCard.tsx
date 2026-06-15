import { Wallet, Store, CheckCircle } from 'lucide-react'
import { useCheck } from '../../hooks/useCheck'

function formatCurrency(amount: number, currency?: string): string {
  const curr = currency ?? 'RUB'
  try {
    return new Intl.NumberFormat('ru-RU', {
      style: 'currency',
      currency: curr,
      minimumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${amount.toFixed(2)} ${curr}`
  }
}

function Skeleton({ className }: { className?: string }) {
  return (
    <div className={`animate-pulse rounded bg-bg-elevated ${className ?? ''}`} />
  )
}

export function BalanceCard() {
  const { data, isLoading, isError, error } = useCheck()

  if (isLoading) {
    return (
      <div className="bg-bg-card border border-border rounded-xl p-6">
        <div className="flex items-center gap-3 mb-6">
          <Skeleton className="w-10 h-10 rounded-lg" />
          <Skeleton className="w-24 h-5" />
        </div>
        <Skeleton className="w-48 h-10 mb-2" />
        <Skeleton className="w-32 h-4 mb-6" />
        <div className="grid grid-cols-2 gap-4">
          <Skeleton className="h-16 rounded-lg" />
          <Skeleton className="h-16 rounded-lg" />
        </div>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="bg-bg-card border border-danger/30 rounded-xl p-6">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-lg bg-danger/10 flex items-center justify-center">
            <Wallet size={20} className="text-danger" />
          </div>
          <span className="text-sm font-medium text-text-secondary">Balance</span>
        </div>
        <p className="text-sm text-danger">
          Failed to load shop info:{' '}
          {error instanceof Error ? error.message : 'Unknown error'}
        </p>
      </div>
    )
  }

  const shop = data?.shop
  const balance = shop?.balance ?? 0
  const currency = shop?.currency
  const status = shop?.status
  const integrationActive = data?.integration?.active

  return (
    <div className="bg-bg-card border border-border rounded-xl p-6">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-lg bg-accent/15 flex items-center justify-center">
          <Wallet size={20} className="text-accent" />
        </div>
        <span className="text-sm font-medium text-text-secondary">Shop Balance</span>
      </div>

      <p className="text-4xl font-bold text-text-primary mb-1">
        {formatCurrency(balance, currency)}
      </p>
      <p className="text-xs text-text-muted mb-6">Current account balance</p>

      <div className="grid grid-cols-2 gap-4">
        <div className="bg-bg-elevated rounded-lg px-4 py-3">
          <div className="flex items-center gap-2 mb-1">
            <Store size={14} className="text-accent" />
            <span className="text-xs text-text-secondary">Shop</span>
          </div>
          <p className="text-sm font-semibold text-text-primary truncate">
            {shop?.name ?? '—'}
          </p>
          {status && (
            <p className="text-xs text-text-muted mt-0.5">{status}</p>
          )}
        </div>

        <div className="bg-bg-elevated rounded-lg px-4 py-3">
          <div className="flex items-center gap-2 mb-1">
            <CheckCircle size={14} className={integrationActive ? 'text-success' : 'text-text-muted'} />
            <span className="text-xs text-text-secondary">Integration</span>
          </div>
          <p className="text-sm font-semibold text-text-primary">
            {integrationActive === undefined
              ? '—'
              : integrationActive
              ? 'Active'
              : 'Inactive'}
          </p>
          {data?.integration?.type && (
            <p className="text-xs text-text-muted mt-0.5">{data.integration.type}</p>
          )}
        </div>
      </div>
    </div>
  )
}
