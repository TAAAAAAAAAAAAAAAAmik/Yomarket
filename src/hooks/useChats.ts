// Re-export order and message hooks for the chat UI.
// The chat list uses orders; chat messages use /chats/{chat_id}/messages.

export {
  useOrders,
  useOrder,
  useSetOrderInWork,
  useConfirmOrder,
  useRefundOrder,
} from './useOrders'

export { useMessages, useSendMessage } from './useMessages'

// Convenience alias: useChats = useOrders (for components that expect useChats)
export { useOrders as useChats } from './useOrders'
