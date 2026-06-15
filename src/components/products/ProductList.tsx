import { useState } from 'react'
import { Search, ChevronLeft, ChevronRight, Package } from 'lucide-react'
import { useProducts } from '../../hooks/useProducts'
import { ProductCard } from './ProductCard'

const PER_PAGE = 20

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-success/15 text-success',
    inactive: 'bg-text-muted/15 text-text-muted',
    pending: 'bg-warning/15 text-warning',
    blocked: 'bg-danger/15 text-danger',
  }
  const style = styles[status.toLowerCase()] ?? 'bg-border text-text-secondary'
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

export function ProductList() {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  const { data, isLoading, isError, error } = useProducts({
    page,
    per_page: PER_PAGE,
    search: search || undefined,
    status: statusFilter || undefined,
  })

  const products = data?.data ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PER_PAGE)

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    setSearch(searchInput)
    setPage(1)
  }

  const handleStatusChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setStatusFilter(e.target.value)
    setPage(1)
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-3">
        <form onSubmit={handleSearch} className="flex gap-2 flex-1">
          <div className="relative flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search products..."
              className="w-full bg-bg-card border border-border rounded-lg pl-9 pr-3 py-2 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors"
            />
          </div>
          <button
            type="submit"
            className="px-4 py-2 bg-accent hover:bg-accent-hover text-white text-sm font-medium rounded-lg transition-colors"
          >
            Search
          </button>
        </form>

        <select
          value={statusFilter}
          onChange={handleStatusChange}
          className="bg-bg-card border border-border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent transition-colors cursor-pointer"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
          <option value="pending">Pending</option>
          <option value="blocked">Blocked</option>
        </select>
      </div>

      {/* Results info */}
      {!isLoading && !isError && (
        <p className="text-xs text-text-muted">
          {total} product{total !== 1 ? 's' : ''} found
          {search ? ` for "${search}"` : ''}
        </p>
      )}

      {/* Table view */}
      {isLoading ? (
        <div className="bg-bg-card border border-border rounded-xl overflow-hidden">
          <div className="divide-y divide-border">
            {[...Array(8)].map((_, i) => (
              <div key={i} className="flex items-center gap-4 px-5 py-3">
                <div className="animate-pulse bg-bg-elevated rounded-lg w-10 h-10 flex-shrink-0" />
                <div className="flex-1 space-y-2">
                  <div className="animate-pulse bg-bg-elevated rounded h-3 w-1/3" />
                  <div className="animate-pulse bg-bg-elevated rounded h-3 w-1/5" />
                </div>
                <div className="animate-pulse bg-bg-elevated rounded h-5 w-16" />
                <div className="animate-pulse bg-bg-elevated rounded h-5 w-20" />
              </div>
            ))}
          </div>
        </div>
      ) : isError ? (
        <div className="bg-bg-card border border-danger/30 rounded-xl p-8 text-center">
          <p className="text-danger text-sm">
            Failed to load products:{' '}
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
        </div>
      ) : products.length === 0 ? (
        <div className="bg-bg-card border border-border rounded-xl p-16 text-center">
          <Package size={40} className="text-text-muted mx-auto mb-4" />
          <p className="text-text-secondary text-sm">No products found</p>
          {search && (
            <button
              onClick={() => { setSearch(''); setSearchInput('') }}
              className="mt-3 text-accent text-xs hover:underline"
            >
              Clear search
            </button>
          )}
        </div>
      ) : (
        <div className="bg-bg-card border border-border rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Product
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider hidden md:table-cell">
                  Category
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Price
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider hidden sm:table-cell">
                  Stock
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Status
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {products.map((product) => {
                const image = product.images?.[0]
                return (
                  <tr key={product.id} className="hover:bg-bg-elevated/50 transition-colors">
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-lg bg-bg-elevated flex items-center justify-center flex-shrink-0 overflow-hidden">
                          {image ? (
                            <img src={image} alt={product.name} className="w-full h-full object-cover" />
                          ) : (
                            <Package size={16} className="text-text-muted" />
                          )}
                        </div>
                        <div className="min-w-0">
                          <p className="font-medium text-text-primary truncate max-w-[180px]">{product.name}</p>
                          <p className="text-xs text-text-muted">#{product.id}</p>
                        </div>
                      </div>
                    </td>
                    <td className="px-5 py-3 text-text-secondary hidden md:table-cell">
                      {product.category ?? '—'}
                    </td>
                    <td className="px-5 py-3 font-semibold text-text-primary whitespace-nowrap">
                      {product.price.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' })}
                    </td>
                    <td className="px-5 py-3 text-text-secondary hidden sm:table-cell">
                      {product.stock !== undefined ? product.stock : '—'}
                    </td>
                    <td className="px-5 py-3">
                      <StatusBadge status={product.status} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-xs text-text-muted">
            Page {page} of {totalPages}
          </p>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              className="p-1.5 rounded-lg text-text-secondary hover:text-text-primary hover:bg-bg-elevated transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={16} />
            </button>
            {[...Array(Math.min(totalPages, 7))].map((_, i) => {
              const pageNum = i + 1
              return (
                <button
                  key={pageNum}
                  onClick={() => setPage(pageNum)}
                  className={`w-8 h-8 rounded-lg text-xs font-medium transition-colors ${
                    page === pageNum
                      ? 'bg-accent text-white'
                      : 'text-text-secondary hover:text-text-primary hover:bg-bg-elevated'
                  }`}
                >
                  {pageNum}
                </button>
              )
            })}
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="p-1.5 rounded-lg text-text-secondary hover:text-text-primary hover:bg-bg-elevated transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
