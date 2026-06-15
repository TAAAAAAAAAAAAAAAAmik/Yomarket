import { MessageSquare, ShoppingBag } from 'lucide-react'
import { Order } from '../../types'

interface OrderChatItemProps {
  order: Order
  selected: boolean
  onClick: () => void
}

function timeAgo(dateStr: string): string {
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMins = Math.floor(diffMs / 60_000)
  if (diffMins < 1) return 'now'
  if (diffMins < 60) return `${diffMins}m`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h`
  const diffDays = Math.floor(diffHours / 24)
  return `${diffDays}d`
}

function statusColor(status: string): string {
  switch (status) {
    case 'new': return 'bg-accent/20 text-accent'
    case 'in_work': return 'bg-warning/20 text-warning'
    case 'completed': return 'bg-success/20 text-success'
    case 'refunded': return 'bg-danger/20 text-danger'
    default: return 'bg-text-muted/20 text-text-muted'
  }
}

export function OrderChatItem({ order, selected, onClick }: OrderChatItemProps) {
  const buyerName = order.buyer?.name ?? `Buyer #${order.buyer?.id ?? '?'}`
  const initials = buyerName
    .split(' ')
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-4 py-3 flex items-start gap-3 transition-colors ${
        selected ? 'bg-accent/10 border-r-2 border-accent' : 'hover:bg-bg-elevated'
      }`}
    >
      {/* Avatar */}
      <div className="w-9 h-9 rounded-full bg-accent/20 flex items-center justify-center flex-shrink-0 text-accent text-xs font-bold">
        {initials || <MessageSquare size={14} />}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-1 mb-0.5">
          <p className="text-sm font-medium text-text-primary truncate">{buyerName}</p>
          <span className="text-xs text-text-muted flex-shrink-0">{timeAgo(order.created_at)}</span>
        </div>
        <div className="flex items-center gap-2">
          <ShoppingBag size={11} className="text-text-muted flex-shrink-0" />
          <p className="text-xs text-text-muted truncate">
            {order.ad?.title ?? `Order #${order.id}`}
          </p>
        </div>
        <div className="flex items-center gap-2 mt-1">
          <span
            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${statusColor(order.status)}`}
          >
            {order.status.replace('_', ' ')}
          </span>
          <span className="text-xs text-text-muted">
            {order.amount.toLocaleString('ru-RU', {
              style: 'currency',
              currency: order.currency ?? 'RUB',
            })}
          </span>
        </div>
      </div>
    </button>
  )
}

// Legacy export alias for any remaining imports
export { OrderChatItem as ChatItem }
