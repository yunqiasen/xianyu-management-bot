import { get, post, put } from '@/utils/request'
const prefix = '/api/v1/orders/commerce'
export interface HistoryJob { id: string; account_id: string; status: string; next_page: number; total_pages: number; imported: number; last_error?: string }
export interface DeliveryIntent { id: string; account_id: string; order_no: string; operation_key: string; line_id: string; confirm_attempt_limit: number; card_type: string; quantity: number; mode: string; content_state: string; confirm_state: string; confirm_attempts: number; next_retry_at: number; not_before: number; last_error?: string; source?: { card_name?: string; card_updated_at?: string; spec_name?: string; spec_value?: string; order_quantity?: number; line_amount?: string; rule_id?: string; rule_version?: number }; pieces?: { part: string; result: string }[]; evidence?: { phase: string; result: string; reference?: string }[] }
export interface RulePreview { line_id: string; rule_id?: string; spec_name?: string; spec_value?: string; card_id: number; name: string; card_type: string; matched: boolean; reason: string; quantity: number }
interface Result<T> { success: boolean; data: T }
const query = (account: string) => account ? `?account_id=${encodeURIComponent(account)}` : ''
export const getHistoryJobs = (account = '') => get<Result<HistoryJob[]>>(`${prefix}/history${query(account)}`)
export const createHistoryJob = (account_id: string) => post<Result<HistoryJob>>(`${prefix}/history`, { account_id })
export const historyAction = (id: string, action: 'step' | 'resume' | 'cancel') => post<Result<HistoryJob>>(`${prefix}/history/${id}/${action}`)
export const getDeliveryIntents = (account = '') => get<Result<DeliveryIntent[]>>(`${prefix}/intents${query(account)}`)
export const confirmDeliveryIntent = (id: string) => post<Result<DeliveryIntent>>(`${prefix}/intents/${id}/confirm`)
export const resendDeliveryIntent = (id: string, request_id: string, reason: string) => post(`${prefix}/intents/${id}/resend`, { acknowledged: true, request_id, reason })
export const reconcileDeliveryIntent = (id: string, phase: 'content' | 'confirm', result: 'confirmed' | 'not_sent', evidence: string) => post(`${prefix}/intents/${id}/reconcile`, { phase, result, evidence })
export const getIntentContent = (id: string) => get<Result<{ texts: string[]; images: string[] }>>(`${prefix}/intents/${id}/content`)
export const previewDeliveryRule = (account: string, order: string) => get<Result<RulePreview[]>>(`${prefix}/rule-preview?account_id=${encodeURIComponent(account)}&order_no=${encodeURIComponent(order)}`)

export const advanceDeliveryIntent = (id: string) => post(`${prefix}/intents/${id}/advance`)
export const querySupplier = (id: string) => post(`${prefix}/intents/${id}/query-supplier`)

export const saveSupplierEvidence = (id: string, texts: string[], evidence: string) => post(`${prefix}/intents/${id}/supplier-evidence`, { texts, evidence })

export interface DeliveryRuleInput { account_id: string | null; card_id: number; keyword: string; match_mode: 'contains' | 'exact' | 'legacy_contains'; delivery_count: number; enabled: boolean; priority: number; description: string | null }
export interface DeliveryRule extends DeliveryRuleInput { id: string; version: number; delivery_times: number }
export const getDeliveryRules = (account = '') => get<Result<DeliveryRule[]>>(`${prefix}/rules${query(account)}`)
export const createDeliveryRule = (data: DeliveryRuleInput) => post<Result<DeliveryRule>>(`${prefix}/rules`, data)
export const updateDeliveryRule = (id: string, data: Partial<DeliveryRuleInput> & { expected_version: number }) => put<Result<DeliveryRule>>(`${prefix}/rules/${id}`, data)
export function displaySpec(value?: string) {
  if (!value) return ''
  try { const parsed = JSON.parse(value); if (Array.isArray(parsed)) return parsed.join(' / ') } catch { /* Plain one-dimensional SKU. */ }
  return value
}
