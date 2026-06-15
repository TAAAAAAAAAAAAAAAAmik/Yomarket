import { useState, useRef, useEffect } from 'react'
import { Send, MessageSquare, Loader2 } from 'lucide-react'
import { useMessages, useSendMessage } from '../../hooks/useMessages'
import { useOrders } from '../../hooks/useOrders'
import { Message } from '../../types'

interface ChatWindowProps {
  chatId: number | string | null
}

function MessageBubble({ message }: { message: Message }) {
  const isShop = message.sender_type === 'shop'
  const isSystem = message.sender_type === 'system'

  const formattedTime = new Date(message.created_at).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  })

  if (isSystem) {
    return (
      <div className="flex justify-center my-2">
        <span className="text-xs text-text-muted bg-bg-elevated px-3 py-1 rounded-full">
          {message.text}
        </span>
      </div>
    )
  }

  return (
    <div className={`flex ${isShop ? 'justify-end' : 'justify-start'}`}>
      <div className="max-w-[72%] group">
        <div
          className={`rounded-2xl px-4 py-2.5 ${
            isShop
              ? 'bg-accent text-white rounded-br-sm'
              : 'bg-bg-elevated text-text-primary rounded-bl-sm'
          }`}
        >
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">{message.text}</p>
        </div>
        <p className={`text-[10px] text-text-muted mt-1 ${isShop ? 'text-right' : 'text-left'}`}>
          {formattedTime}
          {isShop && message.is_read && <span className="ml-1 text-accent-light">&#10003;&#10003;</span>}
        </p>
      </div>
    </div>
  )
}

export function ChatWindow({ chatId }: ChatWindowProps) {
  const [text, setText] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Find the order corresponding to this chatId
  const { data: ordersData } = useOrders()
  const allOrders = ordersData?.pages.flatMap((p) => p.data) ?? []
  const currentOrder = allOrders.find(
    (o) => (o.chat_id != null ? o.chat_id === chatId : o.id === chatId)
  )

  const { data: messagesData, isLoading } = useMessages(chatId ?? '')
  const { mutate: send, isPending: isSending } = useSendMessage(chatId ?? '')

  // Flatten infinite pages to a flat message list
  const messages = messagesData?.pages.flatMap((p) => p.data) ?? []

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  const handleSend = () => {
    const trimmed = text.trim()
    if (!trimmed || !chatId || isSending) return
    send(
      { text: trimmed },
      {
        onSuccess: () => {
          setText('')
          textareaRef.current?.focus()
        },
      }
    )
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (!chatId) {
    return (
      <div className="flex-1 flex items-center justify-center bg-bg-primary">
        <div className="text-center">
          <MessageSquare size={48} className="text-text-muted mx-auto mb-4" />
          <p className="text-text-secondary text-sm">Select an order to open its chat</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 bg-bg-primary">
      {/* Chat header */}
      <div className="px-5 py-3 border-b border-border bg-bg-secondary flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-full bg-accent/20 flex items-center justify-center text-accent text-xs font-bold flex-shrink-0">
            {(currentOrder?.buyer?.name ?? 'B').charAt(0).toUpperCase()}
          </div>
          <div>
            <p className="text-sm font-semibold text-text-primary">
              {currentOrder?.buyer?.name ?? `Chat #${chatId}`}
            </p>
            {currentOrder?.id && (
              <p className="text-xs text-text-muted">Order #{currentOrder.id}</p>
            )}
          </div>
          {currentOrder?.status && (
            <span className="ml-auto text-xs text-text-muted capitalize bg-bg-elevated px-2 py-0.5 rounded-full">
              {currentOrder.status.replace('_', ' ')}
            </span>
          )}
        </div>
      </div>

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
        {isLoading ? (
          <div className="flex items-center justify-center h-full">
            <Loader2 size={24} className="animate-spin text-text-muted" />
          </div>
        ) : messages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p className="text-text-muted text-sm">No messages yet. Start the conversation!</p>
          </div>
        ) : (
          messages.map((msg) => <MessageBubble key={msg.id} message={msg} />)
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="px-4 py-3 border-t border-border bg-bg-secondary flex-shrink-0">
        <div className="flex items-end gap-3">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type a message... (Enter to send, Shift+Enter for new line)"
            rows={1}
            className="flex-1 bg-bg-elevated border border-border rounded-xl px-4 py-2.5 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors resize-none min-h-[42px] max-h-32 overflow-y-auto"
            style={{ height: 'auto' }}
            onInput={(e) => {
              const target = e.currentTarget
              target.style.height = 'auto'
              target.style.height = `${Math.min(target.scrollHeight, 128)}px`
            }}
          />
          <button
            onClick={handleSend}
            disabled={!text.trim() || isSending}
            className="w-10 h-10 flex items-center justify-center rounded-xl bg-accent hover:bg-accent-hover text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
            title="Send message"
          >
            {isSending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Send size={16} />
            )}
          </button>
        </div>
        <p className="text-[10px] text-text-muted mt-1.5 pl-1">
          Press Enter to send &middot; Shift+Enter for new line
        </p>
      </div>
    </div>
  )
}
