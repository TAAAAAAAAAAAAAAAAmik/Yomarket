// Cursor pagination
export interface CursorPaginationMeta {
  next_cursor?: string | null
  prev_cursor?: string | null
  per_page: number
}

export interface CursorPaginationLinks {
  next?: string | null
  prev?: string | null
}

// Check/Auth
export interface CheckResponse {
  shop: {
    id: number
    name: string
    balance?: number
    currency?: string
    status?: string
  }
  integration?: {
    id: number
    type?: string
    active: boolean
  }
}

// Order
export interface Order {
  id: number
  chat_id?: number
  status: 'new' | 'in_work' | 'completed' | 'refunded' | string
  amount: number
  currency?: string
  buyer?: {
    id?: number
    name?: string
  }
  ad?: {
    id: number
    title?: string
  }
  created_at: string
  updated_at?: string
}

export interface OrdersResponse {
  data: Order[]
  meta: CursorPaginationMeta
  links?: CursorPaginationLinks
}

export interface OrdersParams {
  cursor?: string
  per_page?: number
  status?: string
}

// Message
export interface Message {
  id: number
  chat_id: number
  text: string
  sender_type: 'shop' | 'buyer' | 'system'
  created_at: string
  is_read?: boolean
}

export interface MessagesResponse {
  data: Message[]
  meta: CursorPaginationMeta
  links?: CursorPaginationLinks
}

export interface MessagesParams {
  cursor?: string
  per_page?: number
}

export interface SendMessagePayload {
  text: string
}

// Ad (product/listing)
export interface Ad {
  id: number
  title: string
  price: number
  currency?: string
  status?: 'active' | 'inactive' | 'sold' | string
  category?: string
  images?: string[]
  description?: string
  views_count?: number
  created_at: string
  updated_at?: string
}

export interface AdsResponse {
  data: Ad[]
  meta: CursorPaginationMeta
  links?: CursorPaginationLinks
}

export interface AdsParams {
  cursor?: string
  per_page?: number
  status?: string
}
