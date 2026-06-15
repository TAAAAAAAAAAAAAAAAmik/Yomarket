import { Package } from 'lucide-react'
import { Product } from '../../types/product'

interface ProductCardProps {
  product: Product
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-success/15 text-success',
    inactive: 'bg-text-muted/15 text-text-muted',
    pending: 'bg-warning/15 text-warning',
    blocked: 'bg-danger/15 text-danger',
  }
  const style = styles[status.toLowerCase()] ?? 'bg-border text-text-secondary'

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

export function ProductCard({ product }: ProductCardProps) {
  const image = product.images?.[0]

  return (
    <div className="bg-bg-card border border-border rounded-xl p-4 flex gap-4 hover:border-border-light transition-colors">
      {/* Thumbnail */}
      <div className="w-16 h-16 rounded-lg bg-bg-elevated flex items-center justify-center flex-shrink-0 overflow-hidden">
        {image ? (
          <img src={image} alt={product.name} className="w-full h-full object-cover" />
        ) : (
          <Package size={24} className="text-text-muted" />
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2 mb-1">
          <p className="text-sm font-medium text-text-primary truncate">{product.name}</p>
          <StatusBadge status={product.status} />
        </div>

        {product.category && (
          <p className="text-xs text-text-muted mb-1">{product.category}</p>
        )}

        <div className="flex items-center gap-4 mt-2">
          <span className="text-base font-semibold text-text-primary">
            {product.price.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' })}
          </span>
          {product.stock !== undefined && (
            <span className="text-xs text-text-secondary">
              Stock: <span className="text-text-primary font-medium">{product.stock}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
