import { MessageSquare } from 'lucide-react'
import { useOrders } from '../../hooks/useOrders'
import { OrderChatItem } from './ChatItem'

interface ChatListProps {
  selectedChatId: number | string | null
  onSelectChat: (id: number | string) => void
}

export function ChatList({ selectedChatId, onSelectChat }: ChatListProps) {
  // There is no /chats list endpoint. Chats are accessed via orders.
  const { data, isLoading, isError, error } = useOrders()
  const orders = data?.pages.flatMap((p) => p.data) ?? []

  return (
    <div className="w-72 flex-shrink-0 border-r border-border bg-bg-secondary flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <MessageSquare size={16} className="text-text-secondary" />
          <span className="text-sm font-semibold text-text-primary">Order Chats</span>
        </div>
        {orders.length > 0 && (
          <span className="min-w-[20px] h-5 rounded-full bg-accent/20 text-accent text-xs font-bold flex items-center justify-center px-1.5">
            {orders.length}
          </span>
        )}
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="divide-y divide-border">
            {[...Array(6)].map((_, i) => (
              <div key={i} className="px-4 py-3 flex gap-3 items-center">
                <div className="animate-pulse bg-bg-elevated rounded-full w-9 h-9 flex-shrink-0" />
                <div className="flex-1 space-y-2">
                  <div className="animate-pulse bg-bg-elevated rounded h-3 w-2/3" />
                  <div className="animate-pulse bg-bg-elevated rounded h-3 w-1/2" />
                </div>
              </div>
            ))}
          </div>
        ) : isError ? (
          <div className="px-4 py-8 text-center">
            <p className="text-danger text-xs">
              {error instanceof Error ? error.message : 'Failed to load orders'}
            </p>
          </div>
        ) : orders.length === 0 ? (
          <div className="px-4 py-12 text-center">
            <MessageSquare size={28} className="text-text-muted mx-auto mb-3" />
            <p className="text-text-muted text-sm">No orders yet</p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {orders.map((order) => {
              const chatId = order.chat_id ?? order.id
              return (
                <OrderChatItem
                  key={order.id}
                  order={order}
                  selected={chatId === selectedChatId}
                  onClick={() => onSelectChat(chatId)}
                />
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
