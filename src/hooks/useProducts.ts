// Products in YooMarket are called "Ads". There is no /products endpoint.
// Use useAds() and useAd() from ./useAds instead.
// This file re-exports them for backward compatibility.

export { useAds as useProducts, useAd as useProduct } from './useAds'
