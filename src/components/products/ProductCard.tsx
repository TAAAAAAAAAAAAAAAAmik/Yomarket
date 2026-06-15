import { Package, Eye } from 'lucide-react'
import { Ad } from '../../types'

interface AdCardProps {
  ad: Ad
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-success/15 text-success',
    inactive: 'bg-text-muted/15 text-text-muted',
    sold: 'bg-accent/15 text-accent',
    blocked: 'bg-danger/15 text-danger',
  }
  const style = styles[status.toLowerCase()] ?? 'bg-border text-text-secondary'

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

export function AdCard({ ad }: AdCardProps) {
  const image = ad.images?.[0]

  return (
    <div className="bg-bg-card border border-border rounded-xl p-4 flex gap-4 hover:border-border-light transition-colors">
      {/* Thumbnail */}
      <div className="w-16 h-16 rounded-lg bg-bg-elevated flex items-center justify-center flex-shrink-0 overflow-hidden">
        {image ? (
          <img src={image} alt={ad.title} className="w-full h-full object-cover" />
        ) : (
          <Package size={24} className="text-text-muted" />
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2 mb-1">
          <p className="text-sm font-medium text-text-primary truncate">{ad.title}</p>
          {ad.status && <StatusBadge status={ad.status} />}
        </div>

        {ad.category && (
          <p className="text-xs text-text-muted mb-1">{ad.category}</p>
        )}

        <div className="flex items-center gap-4 mt-2">
          <span className="text-base font-semibold text-text-primary">
            {ad.price.toLocaleString('ru-RU', {
              style: 'currency',
              currency: ad.currency ?? 'RUB',
            })}
          </span>
          {ad.views_count !== undefined && (
            <span className="text-xs text-text-secondary flex items-center gap-1">
              <Eye size={12} />
              {ad.views_count.toLocaleString()}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

// Legacy alias
export { AdCard as ProductCard }
