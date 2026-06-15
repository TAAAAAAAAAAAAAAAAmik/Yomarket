import { apiClient } from './client'
import { BalanceResponse } from '../types/balance'

export async function fetchBalance(): Promise<BalanceResponse> {
  const { data } = await apiClient.get<BalanceResponse>('/balance')
  return data
}
