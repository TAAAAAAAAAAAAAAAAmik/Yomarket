import { apiClient } from './client'
import { MessagesResponse, MessagesParams, Message, SendMessagePayload } from '../types'

export async function fetchMessages(
  chatId: number | string,
  params?: MessagesParams
): Promise<MessagesResponse> {
  const { data } = await apiClient.get<MessagesResponse>(`/chats/${chatId}/messages`, { params })
  return data
}

export async function sendMessage(
  chatId: number | string,
  payload: SendMessagePayload
): Promise<Message> {
  const { data } = await apiClient.post<Message>(`/chats/${chatId}/sendMessage`, payload)
  return data
}
