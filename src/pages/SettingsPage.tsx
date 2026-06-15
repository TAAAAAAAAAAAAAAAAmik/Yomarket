import { useState } from 'react'
import { Save, Eye, EyeOff, CheckCircle } from 'lucide-react'
import { Header } from '../components/layout/Header'
import { useAuthStore } from '../stores/authStore'

export function SettingsPage() {
  const { token, shopName, setToken, setShopName } = useAuthStore()

  const [tokenInput, setTokenInput] = useState(token)
  const [shopInput, setShopInput] = useState(shopName)
  const [showToken, setShowToken] = useState(false)
  const [saved, setSaved] = useState(false)

  const handleSave = () => {
    setToken(tokenInput.trim())
    setShopName(shopInput.trim())
    setSaved(true)
    setTimeout(() => setSaved(false), 2500)
  }

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Header title="Settings" subtitle="Configure your YooMarket integration" />

      <main className="flex-1 overflow-y-auto p-6">
        <div className="max-w-lg space-y-6">

          {/* API Token */}
          <div className="bg-bg-card border border-border rounded-xl p-6 space-y-4">
            <div>
              <h2 className="text-sm font-semibold text-text-primary mb-1">API Token</h2>
              <p className="text-xs text-text-secondary">
                Your YooMarket integration token. Found in the YooMarket seller panel under API settings.
              </p>
            </div>

            <div>
              <label className="block text-xs text-text-secondary mb-1.5" htmlFor="token">
                Bearer Token
              </label>
              <div className="relative">
                <input
                  id="token"
                  type={showToken ? 'text' : 'password'}
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  placeholder="Enter your API token..."
                  className="w-full bg-bg-elevated border border-border rounded-lg px-3 py-2.5 pr-10 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors"
                />
                <button
                  type="button"
                  onClick={() => setShowToken((v) => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary transition-colors"
                  title={showToken ? 'Hide token' : 'Show token'}
                >
                  {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </div>
          </div>

          {/* Shop Name */}
          <div className="bg-bg-card border border-border rounded-xl p-6 space-y-4">
            <div>
              <h2 className="text-sm font-semibold text-text-primary mb-1">Shop Name</h2>
              <p className="text-xs text-text-secondary">
                Display name shown in the sidebar. This is local only and does not affect the API.
              </p>
            </div>

            <div>
              <label className="block text-xs text-text-secondary mb-1.5" htmlFor="shopName">
                Shop Name
              </label>
              <input
                id="shopName"
                type="text"
                value={shopInput}
                onChange={(e) => setShopInput(e.target.value)}
                placeholder="My Awesome Shop"
                className="w-full bg-bg-elevated border border-border rounded-lg px-3 py-2.5 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors"
              />
            </div>
          </div>

          {/* Save button */}
          <button
            onClick={handleSave}
            className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-accent hover:bg-accent-hover text-white text-sm font-medium transition-colors"
          >
            {saved ? (
              <>
                <CheckCircle size={16} />
                Saved!
              </>
            ) : (
              <>
                <Save size={16} />
                Save Settings
              </>
            )}
          </button>

          {/* Info box */}
          <div className="bg-bg-elevated border border-border rounded-xl p-4">
            <p className="text-xs text-text-secondary leading-relaxed">
              <span className="text-text-primary font-medium">Note: </span>
              You can also set the token via the <code className="text-accent bg-bg-card px-1 py-0.5 rounded text-xs">VITE_YOOMARKET_TOKEN</code> environment
              variable in a <code className="text-accent bg-bg-card px-1 py-0.5 rounded text-xs">.env</code> file at the project root.
              The token saved here takes precedence over the environment variable after the first save.
            </p>
          </div>
        </div>
      </main>
    </div>
  )
}
