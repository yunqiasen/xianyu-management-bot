import { get, post, put, del } from '@/utils/request'
export interface ReplyPolicy { strategy: 'legacy' | 'ai_first'; version: number; block_personal: boolean; block_platform: boolean }
export interface ExclusiveReply { id?: string; item_id: string; content: string; image_url: string; enabled: boolean; version: number }
export interface AdvancedFilter { all_accounts?: boolean; pause_minutes?: number | null; id?: string; pattern: string; match_mode: 'contains' | 'exact' | 'regex'; source: 'user' | 'system' | 'ai' | 'all'; item_id: string; actions: string[]; enabled: boolean; version: number }
export interface ReplyImage { id: string; url: string; size: number; width: number; height: number }
const base = (account: string) => `/api/v1/chat-new/reply-controls/${encodeURIComponent(account)}`
const data = async <T>(result: Promise<{ success: boolean; data: T; message?: string }>): Promise<T> => {
  const res = await result
  if (!res.success) throw new Error(res.message || '操作未完成')
  return res.data
}
export const getReplyPolicy = (a: string) => data<ReplyPolicy>(get(`${base(a)}/policy`))
export const saveReplyPolicy = (a: string, value: ReplyPolicy) => data<ReplyPolicy>(put(`${base(a)}/policy`, value))
export const getExclusiveReplies = (a: string) => data<ExclusiveReply[]>(get(`${base(a)}/exclusive`))
export const saveExclusiveReply = (a: string, value: ExclusiveReply) => data(post(`${base(a)}/exclusive`, value))
export const deleteExclusiveReply = (a: string, value: ExclusiveReply) => del(`${base(a)}/exclusive/${value.id}?version=${value.version}`)
export const importExclusiveReplies = (a: string, rows: unknown[]) => post<{ success: boolean; data: { saved: number; errors: { row: number; message: string }[] } }>(`${base(a)}/exclusive/import`, { rows })
export const getAdvancedFilters = (a: string) => data<AdvancedFilter[]>(get(`${base(a)}/filters`))
export const saveAdvancedFilter = (a: string, value: AdvancedFilter) => data(value.id ? put(`${base(a)}/filters/${value.id}`, value) : post(`${base(a)}/filters`, value))
export const deleteAdvancedFilter = (a: string, value: AdvancedFilter) => del(`${base(a)}/filters/${value.id}?version=${value.version}`)
export const getReplyImages = (a: string) => data<ReplyImage[]>(get(`${base(a)}/images`))
export const uploadReplyImage = (a: string, file: File) => { const form = new FormData(); form.append('image', file); return data<ReplyImage>(post(`${base(a)}/images`, form)) }
export const deleteReplyImage = (a: string, id: string) => del(`${base(a)}/images/${id}`)
export const imageReferences = (a: string, id: string) => data<{source: string; source_id: string}[]>(get(`${base(a)}/images/${id}/references`))
export interface OutboundVerification { requestId: string; status: 'submitted' | 'confirmed' | 'failed' | 'unknown'; version: number; messageId?: string; verification?: string; evidence?: { source: string; messageId: string; status: string } }
export const verifyOutboundReply = (a: string, chat: string, request: string) => data<OutboundVerification>(post(`${base(a)}/outbound/${encodeURIComponent(chat)}/${encodeURIComponent(request)}/verify`, {}))
export const syncPlatformReplyBlacklist = (account: string, after = 0) => post<{success: boolean; data: {synced: number; nextCursor: number; hasMore: boolean; errors: {chat_id: string; message: string}[]}}>(`${base(account)}/platform-blacklist/sync`, {after, limit: 20})
