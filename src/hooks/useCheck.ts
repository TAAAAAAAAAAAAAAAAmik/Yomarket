import { useQuery } from '@tanstack/react-query'
import { checkToken } from '../api/auth'
import { useAuthStore } from '../stores/authStore'

export const CHECK_QUERY_KEY = ['check'] as const

export function useCheck() {
  const token = useAuthStore((s) => s.token)

  return useQuery({
    queryKey: CHECK_QUERY_KEY,
    queryFn: checkToken,
    staleTime: 60_000,
    refetchInterval: 120_000,
    enabled: !!token,
  })
}
