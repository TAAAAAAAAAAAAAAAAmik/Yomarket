import { apiClient } from './client'
import { CheckResponse } from '../types'

export async function checkToken(): Promise<CheckResponse> {
  const { data } = await apiClient.get<CheckResponse>('/check')
  return data
}
