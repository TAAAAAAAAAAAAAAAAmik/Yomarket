import { MessageSquare } from 'lucide-react'
import { Chat } from '../../types/chat'

interface ChatItemProps {
  chat: Chat
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

export function ChatItem({ chat, selected, onClick }: ChatItemProps) {
  const initials = (chat.buyer_name ?? 'B')
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
          <p className="text-sm font-medium text-text-primary truncate">
            {chat.buyer_name ?? `Order #${chat.order_id ?? chat.id}`}
          </p>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            {chat.last_message_at && (
              <span className="text-xs text-text-muted">{timeAgo(chat.last_message_at)}</span>
            )}
            {(chat.unread_count ?? 0) > 0 && (
              <span className="min-w-[18px] h-[18px] rounded-full bg-accent text-white text-[10px] font-bold flex items-center justify-center px-1">
                {chat.unread_count}
              </span>
            )}
          </div>
        </div>
        {chat.last_message && (
          <p className="text-xs text-text-muted truncate">{chat.last_message}</p>
        )}
        {chat.order_id && (
          <p className="text-xs text-text-muted mt-0.5">Order #{chat.order_id}</p>
        )}
      </div>
    </button>
  )
}
