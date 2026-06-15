import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchOrders,
  fetchOrder,
  setOrderInWork,
  confirmOrder,
  refundOrder,
} from '../api/orders'
import { OrdersParams } from '../types'

export const ORDERS_QUERY_KEY = ['orders'] as const

export function useOrders(params?: Omit<OrdersParams, 'cursor'>) {
  return useInfiniteQuery({
    queryKey: [...ORDERS_QUERY_KEY, params],
    queryFn: ({ pageParam }) => fetchOrders({ ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor ?? undefined,
    staleTime: 30_000,
    refetchInterval: 30_000,
  })
}

export function useOrder(orderId: number | string) {
  return useQuery({
    queryKey: [...ORDERS_QUERY_KEY, orderId],
    queryFn: () => fetchOrder(orderId),
    staleTime: 30_000,
    enabled: !!orderId,
  })
}

export function useSetOrderInWork() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (orderId: number | string) => setOrderInWork(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ORDERS_QUERY_KEY })
    },
  })
}

export function useConfirmOrder() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (orderId: number | string) => confirmOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ORDERS_QUERY_KEY })
    },
  })
}

export function useRefundOrder() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (orderId: number | string) => refundOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ORDERS_QUERY_KEY })
    },
  })
}
