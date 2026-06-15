import { apiClient } from './client'
import { Product, ProductsResponse, ProductsParams } from '../types/product'

export async function fetchProducts(params?: ProductsParams): Promise<ProductsResponse> {
  const { data } = await apiClient.get<ProductsResponse>('/products', { params })
  return data
}

export async function fetchProduct(id: number | string): Promise<Product> {
  const { data } = await apiClient.get<Product>(`/products/${id}`)
  return data
}
