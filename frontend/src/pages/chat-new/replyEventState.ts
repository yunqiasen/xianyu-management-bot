import type { ChatMessage } from '@/api/chatNew'
export function mergeChatMessage(messages: ChatMessage[], incoming: ChatMessage): ChatMessage[] {
  const index = incoming.messageId ? messages.findIndex(m => m.messageId === incoming.messageId) : -1
  if (index < 0) return [...messages, incoming]
  if ((messages[index].version || 0) >= (incoming.version || 0)) return messages
  return messages.map((message, i) => i === index ? incoming : message)
}

export function applyOutboundVerification(message: ChatMessage, result: {requestId: string; version: number; status: ChatMessage['status']; verification?: string}): ChatMessage {
  if (message.messageId !== `out:${result.requestId}` || result.version < (message.version || 0)) return message
  return { ...message, status: result.status, version: result.version, failed: result.status === 'failed', failReason: result.verification }
}

export const chatCacheKey = (accountId: string, identity: string) => JSON.stringify([accountId, identity])
export const chatScopeMatches = (account: string, chat: string, activeAccount: string, activeChat: string) => account === activeAccount && chat === activeChat
