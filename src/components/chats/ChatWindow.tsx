import { useState, useRef, useEffect } from 'react'
import { Send, MessageSquare, Loader2 } from 'lucide-react'
import { useMessages, useSendMessage } from '../../hooks/useChats'
import { useChats } from '../../hooks/useChats'
import { Message } from '../../types/chat'

interface ChatWindowProps {
  chatId: number | string | null
}

function MessageBubble({ message }: { message: Message }) {
  const isSeller = message.sender === 'seller'
  const isSystem = message.sender === 'system'

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
    <div className={`flex ${isSeller ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[72%] group`}>
        <div
          className={`rounded-2xl px-4 py-2.5 ${
            isSeller
              ? 'bg-accent text-white rounded-br-sm'
              : 'bg-bg-elevated text-text-primary rounded-bl-sm'
          }`}
        >
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">{message.text}</p>
        </div>
        <p className={`text-[10px] text-text-muted mt-1 ${isSeller ? 'text-right' : 'text-left'}`}>
          {formattedTime}
          {isSeller && message.read && <span className="ml-1 text-accent-light">✓✓</span>}
        </p>
      </div>
    </div>
  )
}

export function ChatWindow({ chatId }: ChatWindowProps) {
  const [text, setText] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const { data: chatsData } = useChats()
  const currentChat = chatsData?.data?.find((c) => c.id === chatId)

  const { data: messagesData, isLoading } = useMessages(chatId ?? '')
  const { mutate: send, isPending: isSending } = useSendMessage(chatId ?? '')

  const messages = messagesData?.data ?? []

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
          <p className="text-text-secondary text-sm">Select a chat to start messaging</p>
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
            {(currentChat?.buyer_name ?? 'B').charAt(0).toUpperCase()}
          </div>
          <div>
            <p className="text-sm font-semibold text-text-primary">
              {currentChat?.buyer_name ?? `Chat #${chatId}`}
            </p>
            {currentChat?.order_id && (
              <p className="text-xs text-text-muted">Order #{currentChat.order_id}</p>
            )}
          </div>
          {currentChat?.status && (
            <span className="ml-auto text-xs text-text-muted capitalize bg-bg-elevated px-2 py-0.5 rounded-full">
              {currentChat.status}
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
          Press Enter to send · Shift+Enter for new line
        </p>
      </div>
    </div>
  )
}
