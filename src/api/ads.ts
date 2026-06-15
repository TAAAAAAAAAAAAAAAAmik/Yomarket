import { apiClient } from './client'
import { Ad, AdsResponse, AdsParams } from '../types'

export async function fetchAds(params?: AdsParams): Promise<AdsResponse> {
  const { data } = await apiClient.get<AdsResponse>('/ads', { params })
  return data
}

export async function fetchAd(id: number | string): Promise<Ad> {
  const { data } = await apiClient.get<Ad>(`/ads/${id}`)
  return data
}
