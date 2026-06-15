// Products in YooMarket are called "Ads". There is no /products endpoint.
// This file re-exports from api/ads.ts for backward compatibility.
export { fetchAds as fetchProducts, fetchAd as fetchProduct } from './ads'
