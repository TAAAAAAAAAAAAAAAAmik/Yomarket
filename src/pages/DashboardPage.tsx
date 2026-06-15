import { Package, MessageSquare, ShoppingBag } from 'lucide-react'
import { Header } from '../components/layout/Header'
import { BalanceCard } from '../components/balance/BalanceCard'
import { useProducts } from '../hooks/useProducts'
import { useChats } from '../hooks/useChats'
import { ChatItem } from '../components/chats/ChatItem'

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
  const { data: productsData, isLoading: productsLoading } = useProducts({ per_page: 1 })
  const { data: chatsData, isLoading: chatsLoading } = useChats()

  const totalProducts = productsData?.total ?? 0
  const activeProducts = productsData?.data?.filter((p) => p.status === 'active').length ?? 0
  const unreadChats = chatsData?.data?.reduce((sum, c) => sum + (c.unread_count ?? 0), 0) ?? 0
  const recentChats = chatsData?.data?.slice(0, 5) ?? []

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Header title="Dashboard" subtitle="Overview of your YooMarket store" />

      <main className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Balance */}
        <div className="max-w-sm">
          <BalanceCard />
        </div>

        {/* Quick stats */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <StatCard
            label="Total Products"
            value={totalProducts}
            icon={Package}
            color="bg-accent/15 text-accent"
            loading={productsLoading}
          />
          <StatCard
            label="Active Products"
            value={activeProducts}
            icon={ShoppingBag}
            color="bg-success/15 text-success"
            loading={productsLoading}
          />
          <StatCard
            label="Unread Chats"
            value={unreadChats}
            icon={MessageSquare}
            color="bg-warning/15 text-warning"
            loading={chatsLoading}
          />
        </div>

        {/* Recent chats */}
        <div>
          <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-3">
            Recent Chats
          </h2>
          <div className="bg-bg-card border border-border rounded-xl overflow-hidden">
            {chatsLoading ? (
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
            ) : recentChats.length === 0 ? (
              <div className="px-4 py-10 text-center text-text-muted text-sm">
                No chats yet
              </div>
            ) : (
              <div className="divide-y divide-border">
                {recentChats.map((chat) => (
                  <ChatItem key={chat.id} chat={chat} selected={false} onClick={() => {}} />
                ))}
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
