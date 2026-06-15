import { Header } from '../components/layout/Header'
import { AdList } from '../components/products/ProductList'

export function ProductsPage() {
  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Header title="Ads" subtitle="Your marketplace listings" />
      <main className="flex-1 overflow-y-auto p-6">
        <AdList />
      </main>
    </div>
  )
}
