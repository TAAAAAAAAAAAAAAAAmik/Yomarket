import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  token: string
  shopName: string
  setToken: (token: string) => void
  setShopName: (name: string) => void
  clearAuth: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: (import.meta.env['VITE_YOOMARKET_TOKEN'] as string | undefined) ?? '',
      shopName: 'YooMarket Shop',
      setToken: (token) => set({ token }),
      setShopName: (shopName) => set({ shopName }),
      clearAuth: () => set({ token: '', shopName: '' }),
    }),
    {
      name: 'yoomarket-auth',
    }
  )
)
