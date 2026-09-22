export type AIProtocol = 'openai_compatible' | 'responses' | 'azure' | 'anthropic' | 'gemini' | 'dashscope_app'
export interface AISettings {
  ai_enabled: boolean
  provider_type: AIProtocol
  base_url: string
  api_key: string
  api_key_configured: boolean
  model_name: string
  azure_deployment: string
  azure_api_version: string
  azure_auth_mode: 'api_key' | 'bearer'
  app_id: string
  timeout_seconds: number
  max_tokens: number
  temperature: number
  max_context_chars: number
  config_version: number
  custom_prompts: string
  max_discount_percent: number
  max_discount_amount: number
  max_bargain_rounds: number
  ai_time_range_start: string
  ai_time_range_end: string
  manual_reply_ai_pause_enabled: boolean
  manual_reply_ai_pause_minutes: number
}
export type AISettingsPatch = Partial<AISettings> & { clear_api_key?: boolean }
export interface AIEnvelope<T> { success: boolean; message?: string; data?: T }
export interface AIPreset { id: string; name: string; settings: AISettings }
export interface AIAccountResult { account_id: string; success: boolean; message?: string; config_version?: number }
export interface AIProbeResult { stage: string; reply?: string; status_code?: number; provider_code?: string; call_id?: string; config_version?: number; trimmed_messages?: number }
export const AI_PROTOCOLS: { value: AIProtocol; label: string; base: string }[] = [
  { value: 'openai_compatible', label: 'OpenAI 兼容 / Ollama', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { value: 'responses', label: 'OpenAI Responses', base: 'https://api.openai.com/v1' },
  { value: 'azure', label: 'Azure OpenAI', base: '' },
  { value: 'anthropic', label: 'Anthropic Messages', base: 'https://api.anthropic.com' },
  { value: 'gemini', label: 'Google Gemini', base: 'https://generativelanguage.googleapis.com' },
  { value: 'dashscope_app', label: 'DashScope 应用', base: 'https://dashscope.aliyuncs.com' },
]

export interface BargainingDiagnosticRecord {
  log_id: number
  chat_id: string
  stage?: string
  reason?: string
  bargaining: { count: number; is_bargaining?: boolean; intent?: string; missing_event_ids?: number }
  history_window?: {
    selected_messages?: number; trimmed_messages?: number; manual_messages?: number
    first_event_id?: string | null; last_event_id?: string | null; reason?: string
  }
}
export interface BargainingDiagnosticsData {
  candidate_enabled: boolean
  records: BargainingDiagnosticRecord[]
}
