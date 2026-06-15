export interface Balance {
  balance: number
  currency: string
  pending?: number
  available?: number
}

export interface BalanceResponse {
  balance: number
  currency: string
  pending?: number
  available?: number
}
