import { Package, MessageSquare, ShoppingBag } from 'lucide-react'
import { Header } from '../components/layout/Header'
import { BalanceCard } from '../components/balance/BalanceCard'
import { useAds } from '../hooks/useAds'
import { useOrders } from '../hooks/useOrders'
import { OrderChatItem } from '../components/chats/ChatItem'

function StatCard({
  label,
  value,
  icon: Icon,
  color,
  loading,
}: {
  label: string
  value: number | string
  icon: React.ElementType
  color: string
  loading?: boolean
}) {
  return (
    <div className="bg-bg-card border border-border rounded-xl p-5 flex items-center gap-4">
      <div className={`w-11 h-11 rounded-lg flex items-center justify-center flex-shrink-0 ${color}`}>
        <Icon size={20} />
      </div>
      <div>
        <p className="text-xs text-text-secondary mb-0.5">{label}</p>
        {loading ? (
          <div className="animate-pulse bg-bg-elevated rounded h-6 w-16" />
        ) : (
          <p className="text-2xl font-bold text-text-primary">{value}</p>
        )}
      </div>
    </div>
  )
}

export function DashboardPage() {
  const { data: adsData, isLoading: adsLoading } = useAds({ per_page: 1 })
  const { data: ordersData, isLoading: ordersLoading } = useOrders({ per_page: 5 })

  const totalAds = adsData?.pages[0]?.data.length ?? 0
  const activeAds =
    adsData?.pages.flatMap((p) => p.data).filter((a) => a.status === 'active').length ?? 0
  const recentOrders = ordersData?.pages.flatMap((p) => p.data).slice(0, 5) ?? []
  const totalOrders = recentOrders.length

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Header title="Dashboard" subtitle="Overview of your YooMarket store" />

      <main className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Balance / Shop Info */}
        <div className="max-w-sm">
          <BalanceCard />
        </div>

        {/* Quick stats */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <StatCard
            label="Total Ads"
            value={totalAds}
            icon={Package}
            color="bg-accent/15 text-accent"
            loading={adsLoading}
          />
          <StatCard
            label="Active Ads"
            value={activeAds}
            icon={ShoppingBag}
            color="bg-success/15 text-success"
            loading={adsLoading}
          />
          <StatCard
            label="Recent Orders"
            value={totalOrders}
            icon={MessageSquare}
            color="bg-warning/15 text-warning"
            loading={ordersLoading}
          />
        </div>

        {/* Recent orders (order-based chats) */}
        <div>
          <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-3">
            Recent Orders
          </h2>
          <div className="bg-bg-card border border-border rounded-xl overflow-hidden">
            {ordersLoading ? (
              <div className="divide-y divide-border">
                {[...Array(4)].map((_, i) => (
                  <div key={i} className="px-4 py-3 flex gap-3 items-center">
                    <div className="animate-pulse bg-bg-elevated rounded-full w-9 h-9 flex-shrink-0" />
                    <div className="flex-1 space-y-2">
                      <div className="animate-pulse bg-bg-elevated rounded h-3 w-1/3" />
                      <div className="animate-pulse bg-bg-elevated rounded h-3 w-2/3" />
                    </div>
                  </div>
                ))}
              </div>
            ) : recentOrders.length === 0 ? (
              <div className="px-4 py-10 text-center text-text-muted text-sm">
                No orders yet
              </div>
            ) : (
              <div className="divide-y divide-border">
                {recentOrders.map((order) => (
                  <OrderChatItem key={order.id} order={order} selected={false} onClick={() => {}} />
                ))}
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
