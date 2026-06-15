import { useState } from 'react'
import { Search, Package, ChevronDown, Eye } from 'lucide-react'
import { useAds } from '../../hooks/useAds'
import { Ad } from '../../types'

const PER_PAGE = 20

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-success/15 text-success',
    inactive: 'bg-text-muted/15 text-text-muted',
    sold: 'bg-accent/15 text-accent',
    blocked: 'bg-danger/15 text-danger',
  }
  const style = styles[status.toLowerCase()] ?? 'bg-border text-text-secondary'
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

function AdRow({ ad }: { ad: Ad }) {
  const image = ad.images?.[0]
  return (
    <tr className="hover:bg-bg-elevated/50 transition-colors">
      <td className="px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-bg-elevated flex items-center justify-center flex-shrink-0 overflow-hidden">
            {image ? (
              <img src={image} alt={ad.title} className="w-full h-full object-cover" />
            ) : (
              <Package size={16} className="text-text-muted" />
            )}
          </div>
          <div className="min-w-0">
            <p className="font-medium text-text-primary truncate max-w-[180px]">{ad.title}</p>
            <p className="text-xs text-text-muted">#{ad.id}</p>
          </div>
        </div>
      </td>
      <td className="px-5 py-3 text-text-secondary hidden md:table-cell">
        {ad.category ?? '—'}
      </td>
      <td className="px-5 py-3 font-semibold text-text-primary whitespace-nowrap">
        {ad.price.toLocaleString('ru-RU', {
          style: 'currency',
          currency: ad.currency ?? 'RUB',
        })}
      </td>
      <td className="px-5 py-3 text-text-secondary hidden sm:table-cell">
        {ad.views_count !== undefined ? (
          <span className="flex items-center gap-1">
            <Eye size={12} className="text-text-muted" />
            {ad.views_count.toLocaleString()}
          </span>
        ) : '—'}
      </td>
      <td className="px-5 py-3">
        {ad.status ? <StatusBadge status={ad.status} /> : '—'}
      </td>
    </tr>
  )
}

export function AdList() {
  const [statusFilter, setStatusFilter] = useState('')
  const [searchInput, setSearchInput] = useState('')

  const {
    data,
    isLoading,
    isError,
    error,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useAds({
    per_page: PER_PAGE,
    status: statusFilter || undefined,
  })

  const ads = data?.pages.flatMap((p) => p.data) ?? []

  const filtered = searchInput.trim()
    ? ads.filter((a) => a.title.toLowerCase().includes(searchInput.trim().toLowerCase()))
    : ads

  const handleStatusChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setStatusFilter(e.target.value)
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
          <input
            type="text"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Filter ads by title..."
            className="w-full bg-bg-card border border-border rounded-lg pl-9 pr-3 py-2 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors"
          />
        </div>

        <select
          value={statusFilter}
          onChange={handleStatusChange}
          className="bg-bg-card border border-border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent transition-colors cursor-pointer"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
          <option value="sold">Sold</option>
        </select>
      </div>

      {/* Results info */}
      {!isLoading && !isError && (
        <p className="text-xs text-text-muted">
          {filtered.length} ad{filtered.length !== 1 ? 's' : ''} loaded
          {searchInput ? ` (filtered by "${searchInput}")` : ''}
        </p>
      )}

      {/* Table */}
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
            Failed to load ads:{' '}
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-bg-card border border-border rounded-xl p-16 text-center">
          <Package size={40} className="text-text-muted mx-auto mb-4" />
          <p className="text-text-secondary text-sm">No ads found</p>
          {searchInput && (
            <button
              onClick={() => setSearchInput('')}
              className="mt-3 text-accent text-xs hover:underline"
            >
              Clear filter
            </button>
          )}
        </div>
      ) : (
        <div className="bg-bg-card border border-border rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Ad
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider hidden md:table-cell">
                  Category
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Price
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider hidden sm:table-cell">
                  Views
                </th>
                <th className="text-left px-5 py-3 text-xs font-semibold text-text-secondary uppercase tracking-wider">
                  Status
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filtered.map((ad) => (
                <AdRow key={ad.id} ad={ad} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Load more (cursor pagination) */}
      {hasNextPage && (
        <div className="flex justify-center">
          <button
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
            className="flex items-center gap-2 px-4 py-2 bg-bg-card border border-border rounded-lg text-sm text-text-secondary hover:text-text-primary hover:border-border-light transition-colors disabled:opacity-50"
          >
            <ChevronDown size={15} />
            {isFetchingNextPage ? 'Loading...' : 'Load more ads'}
          </button>
        </div>
      )}
    </div>
  )
}

// Legacy alias in case anything imports ProductList
export { AdList as ProductList }
