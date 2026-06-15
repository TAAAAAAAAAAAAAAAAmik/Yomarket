import { useQuery } from '@tanstack/react-query'
import { fetchProducts, fetchProduct } from '../api/products'
import { ProductsParams } from '../types/product'

export const PRODUCTS_QUERY_KEY = ['products'] as const

export function useProducts(params?: ProductsParams) {
  return useQuery({
    queryKey: [...PRODUCTS_QUERY_KEY, params],
    queryFn: () => fetchProducts(params),
    staleTime: 60_000,
  })
}

export function useProduct(id: number | string) {
  return useQuery({
    queryKey: [...PRODUCTS_QUERY_KEY, id],
    queryFn: () => fetchProduct(id),
    staleTime: 60_000,
    enabled: !!id,
  })
}
