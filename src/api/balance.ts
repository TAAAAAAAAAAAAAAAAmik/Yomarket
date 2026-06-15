// There is no /balance endpoint in the YooMarket API.
// Balance and shop info come from GET /check — use api/auth.ts checkToken() instead.
// This file re-exports checkToken for backward compatibility.
export { checkToken as fetchBalance } from './auth'
