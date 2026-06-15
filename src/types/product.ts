export interface Product {
  id: number | string
  name: string
  price: number
  status: string
  category?: string
  images?: string[]
  stock?: number
  description?: string
  created_at?: string
  updated_at?: string
}

export interface ProductsResponse {
  data: Product[]
  total: number
  page: number
  per_page: number
}

export interface ProductsParams {
  page?: number
  per_page?: number
  search?: string
  status?: string
}
