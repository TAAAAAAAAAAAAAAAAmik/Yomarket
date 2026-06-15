import { apiClient } from './client'
import { Order, OrdersResponse, OrdersParams } from '../types'

export async function fetchOrders(params?: OrdersParams): Promise<OrdersResponse> {
  const { data } = await apiClient.get<OrdersResponse>('/orders', { params })
  return data
}

export async function fetchOrder(orderId: number | string): Promise<Order> {
  const { data } = await apiClient.get<Order>(`/orders/${orderId}`)
  return data
}

export async function setOrderInWork(orderId: number | string): Promise<Order> {
  const { data } = await apiClient.post<Order>(`/orders/${orderId}/work`)
  return data
}

export async function confirmOrder(orderId: number | string): Promise<Order> {
  const { data } = await apiClient.post<Order>(`/orders/${orderId}/confirm`)
  return data
}

export async function refundOrder(orderId: number | string): Promise<Order> {
  const { data } = await apiClient.post<Order>(`/orders/${orderId}/refund`)
  return data
}
