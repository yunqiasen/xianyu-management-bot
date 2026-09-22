import { get, post, del, put } from '@/utils/request'
import type { ApiResponse } from '@/types'

export interface CredentialJob { id: string; kind: string; status: string; expires_at: number; reason?: string }
export interface AccountRuntime {
  enabled: boolean; business_state: string; connection_state?: string; reason?: string
  last_success_at?: number; next_retry_at: number; credential_version: number
  config_version: number; generation: number; recovery_attempts: number
  pending_consumers: string[]; jobs: CredentialJob[]; has_password: boolean
}
const path = (id: string) => `/api/v1/cookies/${encodeURIComponent(id)}`
export const readAccountRuntime = (id: string) => get<ApiResponse<AccountRuntime>>(`${path(id)}/runtime`)
export const submitCredentialJob = (id: string, value: string, version: number) =>
  post<ApiResponse<CredentialJob>>(`${path(id)}/credential-jobs`, { value, expected_version: version })
export const cancelCredentialJob = (id: string, job: string) =>
  del<ApiResponse<CredentialJob>>(`${path(id)}/credential-jobs/${encodeURIComponent(job)}`)
export const clearSavedPassword = (id: string) =>
  put<ApiResponse>(`${path(id)}/login-info`, { clear_fields: ['login_password'] })
export const previewAccountDeletion = (id: string) =>
  get<ApiResponse<{can_delete: boolean; unfinished_orders: number; unfinished_deliveries: number; pending_replies: number; pending_operations: number; active_jobs: number}>>(`${path(id)}/delete-preview`)

const verificationPath = (id = '') => `/api/v1/password-login/verification${id ? '/' + encodeURIComponent(id) : ''}`
export const startAccountVerification = (account_id: string) =>
  post<{ success: boolean; session_id: string; expires_at: number }>(verificationPath(), { account_id })
export const controlAccountVerification = (id: string, command: { action: string; x?: number; y?: number; points?: { x: number; y: number }[]; text?: string; key?: string }) => post(verificationPath(id) + '/control', command)
export const finishAccountVerification = (id: string) => post<{ success: boolean }>(verificationPath(id) + '/complete')
export const cancelAccountVerification = (id: string) => del(verificationPath(id))
export const statusAccountVerification = (id: string) => get<CredentialJob>(verificationPath(id))
export const screenshotAccountVerification = async (id: string) => {
  const { default: request } = await import('@/utils/request')
  const response = await request.get<Blob>(verificationPath(id) + '/screenshot', { responseType: 'blob' })
  return response.data
}

export interface RequestPolicyValues {
  min_interval_seconds: number
  max_interval_seconds?: number
  requests_per_minute?: number
}
export interface AccountRequestPolicyState {
  configured: boolean
  values: Partial<RequestPolicyValues>
  effective_interval_seconds: number | null
  config_version: number
  pending_consumers: string[]
  source: string
}
export const readAccountRequestPolicy = (id: string) =>
  get<ApiResponse<AccountRequestPolicyState>>(`${path(id)}/request-policy`)
export const saveAccountRequestPolicy = (id: string, expected_config_version: number, values: RequestPolicyValues) =>
  put<ApiResponse<AccountRequestPolicyState>>(`${path(id)}/request-policy`, { expected_config_version, ...values })
export const applyAccountConfiguration = (id: string, config_version: number) =>
  post<ApiResponse<{ complete: boolean; pending_consumers: string[] }>>(`${path(id)}/configuration/reload`, { config_version })

export type ConfigurationScope = 'account' | 'user' | 'system'
export interface TypedConfiguration {
  account_id: string
  schema: Record<string, { label: string; type: 'integer' | 'boolean' | 'string' | 'object'; default: unknown; minimum: number | null; maximum: number | null; nullable: boolean }>
  values: Record<string, { value: unknown; source: ConfigurationScope | 'default'; inherited: boolean; error?: string }>
  versions: Record<ConfigurationScope, number>
  scope_values: Record<'user' | 'system', Record<string, unknown>>
  can_edit_system: boolean
  pending_consumers: string[]
  affected_accounts?: number
  unknown_keys: string[]
}
export const readTypedConfiguration = (id: string) =>
  get<ApiResponse<TypedConfiguration>>(`${path(id)}/configuration/effective`)
export const saveTypedConfiguration = (id: string, payload: {
  scope: ConfigurationScope; key: string; action: 'set' | 'inherit'; value?: unknown; expected_version: number
}) => put<ApiResponse<TypedConfiguration>>(`${path(id)}/configuration/settings`, payload)
export const announceConfigurationChange = (accountId: string) =>
  window.dispatchEvent(new CustomEvent('account-configuration-changed', { detail: accountId }))
