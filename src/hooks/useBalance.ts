// Balance data comes from the /check endpoint, not a dedicated /balance endpoint.
// This hook re-exports check data shaped for balance display.
export { useCheck as useBalance, CHECK_QUERY_KEY as BALANCE_QUERY_KEY } from './useCheck'
