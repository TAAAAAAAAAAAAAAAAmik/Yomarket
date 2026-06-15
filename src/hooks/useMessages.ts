import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchMessages, sendMessage } from '../api/chats'
import { MessagesParams, SendMessagePayload } from '../types'

export const MESSAGES_QUERY_KEY = ['messages'] as const

export function useMessages(
  chatId: number | string | null | undefined,
  params?: Omit<MessagesParams, 'cursor'>
) {
  return useInfiniteQuery({
    queryKey: [...MESSAGES_QUERY_KEY, chatId, params],
    queryFn: ({ pageParam }) =>
      fetchMessages(chatId!, { ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor ?? undefined,
    staleTime: 10_000,
    refetchInterval: 10_000,
    enabled: !!chatId,
  })
}

export function useSendMessage(chatId: number | string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (payload: SendMessagePayload) => sendMessage(chatId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: [...MESSAGES_QUERY_KEY, chatId],
      })
    },
  })
}
