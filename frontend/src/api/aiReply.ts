import { get, post, put, del } from '@/utils/request'
import type { AISettings, AISettingsPatch, AIEnvelope, AIPreset, AIAccountResult, AIProbeResult } from '@/types/aiReply'
const base = '/api/v1/ai-reply-settings'
const id = encodeURIComponent
export const loadAISettings = (accountId: string) => get<AISettings>(`${base}/${id(accountId)}`)
export const saveAISettings = (accountId: string, settings: AISettingsPatch) => put<AIEnvelope<AISettings>>(`${base}/${id(accountId)}`, settings)
export const probeAISettings = (accountId: string) => post<AIEnvelope<AIProbeResult>>(`/api/v1/ai-reply-test/${id(accountId)}`, {}, { timeout: 125000 })
export const listAIPresets = () => get<AIEnvelope<AIPreset[]>>(`${base}/presets`)
export const createAIPreset = (name: string, settings: AISettingsPatch, sourceAccountId: string) => post<AIEnvelope<AIPreset>>(`${base}/presets`, { name, settings, source_account_id: sourceAccountId })
export const updateAIPreset = (presetId: string, name: string, settings?: AISettingsPatch) => put<AIEnvelope<AIPreset>>(`${base}/presets/${id(presetId)}`, { name, settings })
export const deleteAIPreset = (presetId: string) => del<AIEnvelope<never>>(`${base}/presets/${id(presetId)}`)
export const applyAIPreset = (presetId: string, accountIds: string[]) => post<AIEnvelope<{ results: AIAccountResult[] }>>(`${base}/presets/${id(presetId)}/apply`, { account_ids: accountIds })
export const loadAIModels = (accountId: string, settings: AISettingsPatch) => post<AIEnvelope<{ models: { id: string; name: string }[] }>>(`${base}/models`, { account_id: accountId, provider_type: settings.provider_type, api_key: settings.api_key, base_url: settings.base_url })
export const loadBargainingDiagnostics = (accountId: string) => get<AIEnvelope<import('@/types/aiReply').BargainingDiagnosticsData>>(`${base}/${id(accountId)}/bargaining-diagnostics`)
