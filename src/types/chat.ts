export interface Chat {
  id: number | string
  order_id?: number | string
  buyer_name?: string
  last_message?: string
  last_message_at?: string
  unread_count?: number
  status?: string
}

export interface Message {
  id: number | string
  chat_id: number | string
  text: string
  sender: 'buyer' | 'seller' | 'system'
  created_at: string
  read?: boolean
}

export interface ChatsResponse {
  data: Chat[]
  total: number
}

export interface MessagesResponse {
  data: Message[]
}

export interface SendMessagePayload {
  text: string
}
