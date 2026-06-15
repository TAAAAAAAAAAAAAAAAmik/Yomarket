import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { fetchAds, fetchAd } from '../api/ads'
import { AdsParams } from '../types'

export const ADS_QUERY_KEY = ['ads'] as const

export function useAds(params?: Omit<AdsParams, 'cursor'>) {
  return useInfiniteQuery({
    queryKey: [...ADS_QUERY_KEY, params],
    queryFn: ({ pageParam }) => fetchAds({ ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor ?? undefined,
    staleTime: 60_000,
  })
}

export function useAd(id: number | string) {
  return useQuery({
    queryKey: [...ADS_QUERY_KEY, id],
    queryFn: () => fetchAd(id),
    staleTime: 60_000,
    enabled: !!id,
  })
}
