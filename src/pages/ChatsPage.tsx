import { useState } from 'react'
import { Header } from '../components/layout/Header'
import { ChatList } from '../components/chats/ChatList'
import { ChatWindow } from '../components/chats/ChatWindow'

export function ChatsPage() {
  const [selectedChatId, setSelectedChatId] = useState<number | string | null>(null)

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Header title="Chats" subtitle="Communicate with your buyers" />
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <ChatList selectedChatId={selectedChatId} onSelectChat={setSelectedChatId} />
        <ChatWindow chatId={selectedChatId} />
      </div>
    </div>
  )
}
