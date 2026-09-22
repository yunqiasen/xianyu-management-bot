import BargainingDiagnostics from '@/pages/ai-settings/BargainingDiagnostics'
import { useEffect, useState } from 'react'
import { AI_PROTOCOLS, type AISettings, type AISettingsPatch, type AIPreset, type AIAccountResult, type AIProbeResult } from '@/types/aiReply'
import { loadAISettings, saveAISettings, probeAISettings, listAIPresets, createAIPreset, updateAIPreset, deleteAIPreset, applyAIPreset, loadAIModels } from '@/api/aiReply'

const input = 'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-teal-600 focus:outline-none focus:ring-1 focus:ring-teal-600'
const button = 'rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-40'
const primary = 'rounded-lg bg-teal-700 px-4 py-2 text-sm text-white hover:bg-teal-800 disabled:opacity-40'

export function AIProtocolFields({ settings, onChange }: { settings: Partial<AISettings>; onChange: (patch: AISettingsPatch) => void }) {
  const provider = settings.provider_type || 'openai_compatible'
  const text = (label: string, key: keyof AISettings, type = 'text', placeholder = '') => <label className="space-y-1 text-sm">{label}<input className={input} aria-label={label} type={type} autoComplete={type === 'password' ? 'new-password' : 'off'} value={String(settings[key] ?? '')} placeholder={placeholder} onChange={event => onChange({ [key]: event.target.value })} /></label>
  return <div className="grid gap-4 sm:grid-cols-2">
    <label className="space-y-1 text-sm">协议<select className={input} aria-label="协议" value={provider} onChange={event => { const next = AI_PROTOCOLS.find(row => row.value === event.target.value)!; onChange({ provider_type: next.value, base_url: next.base }) }}>{AI_PROTOCOLS.map(row => <option key={row.value} value={row.value}>{row.label}</option>)}</select></label>
    {text('API 地址', 'base_url', 'url', '支持根地址或完整接口地址')}
    {text('API Key', 'api_key', 'password', settings.api_key_configured ? '留空保留已存密钥' : '填写密钥')}
    {provider !== 'dashscope_app' && provider !== 'azure' && text('模型名称', 'model_name')}
    {provider === 'azure' && <>{text('Azure 部署名', 'azure_deployment')}{text('API 版本', 'azure_api_version', 'text', '2024-10-21')}<label className="space-y-1 text-sm">认证方式<select aria-label="认证方式" className={input} value={settings.azure_auth_mode || 'api_key'} onChange={event => onChange({ azure_auth_mode: event.target.value as 'api_key' | 'bearer' })}><option value="api_key">API Key</option><option value="bearer">Bearer / Entra 令牌</option></select></label></>}
    {provider === 'dashscope_app' && text('应用标识', 'app_id')}
    {(['timeout_seconds', 'max_tokens', 'temperature', 'max_context_chars'] as const).map((key, i) => <label key={key} className="space-y-1 text-sm">{['超时（秒）', '最大输出 Token', '温度', '上下文字符预算'][i]}<input className={input} aria-label={key} type="number" step={key === 'temperature' ? .1 : 1} min={key === 'temperature' ? 0 : 1} max={key === 'temperature' ? 2 : key === 'timeout_seconds' ? 120 : undefined} value={settings[key] ?? [60, 512, .7, 24000][i]} onChange={event => onChange({ [key]: Number(event.target.value) })} /></label>)}
  </div>
}

interface Props { accountId: string; accounts?: { id: string; label?: string }[]; onSaved?: () => void; onClose?: () => void }
const stages: Record<string, string> = { success: '连接成功', authentication: '认证失败', http: 'HTTP 错误', provider: '服务商业务错误', parse: '响应格式错误', empty: '空正文', timeout: '请求超时', connection: '连接失败', configuration: '配置错误', budget: '上下文超出预算', internal: '服务内部错误' }

export default function AISettingsPanel({ accountId, accounts = [], onSaved, onClose }: Props) {
  const [settings, setSettings] = useState<AISettings | null>(null)
  const [clearKey, setClearKey] = useState(false)
  const [presets, setPresets] = useState<AIPreset[]>([])
  const [selected, setSelected] = useState('')
  const [presetName, setPresetName] = useState('')
  const [targets, setTargets] = useState<string[]>([accountId])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [probe, setProbe] = useState<AIProbeResult | null>(null)
  const [results, setResults] = useState<AIAccountResult[]>([])
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [dirty, setDirty] = useState(false)
  const update = (patch: AISettingsPatch) => { setSettings(current => current ? { ...current, ...patch } : current); setDirty(true); setProbe(null) }
  const refreshPresets = async () => { const result = await listAIPresets(); if (!result.success) throw new Error(result.message); setPresets(result.data || []) }
  const reload = async () => { setSettings(await loadAISettings(accountId)); setDirty(false); setClearKey(false) }
  useEffect(() => {
    let active = true
    setSettings(null); setProbe(null); setMessage(''); setTargets([accountId]); setResults([]); setDirty(false); setClearKey(false)
    Promise.all([loadAISettings(accountId), listAIPresets()]).then(([config, rows]) => { if (active) { setSettings(config); setPresets(rows.data || []); if (!rows.success) setMessage(rows.message || '预设加载失败') } }).catch(() => { if (active) setMessage('配置加载失败') })
    return () => { active = false }
  }, [accountId])
  const run = async (action: () => Promise<void>) => { setBusy(true); setMessage(''); try { await action() } catch (error) { setMessage(error instanceof Error ? error.message : '操作失败') } finally { setBusy(false) } }
  const save = async () => {
    if (!settings) return false
    const result = await saveAISettings(accountId, { ...settings, clear_api_key: clearKey })
    if (!result.success) throw new Error(result.message || '保存失败')
    if (result.data) setSettings(result.data)
    setClearKey(false); setDirty(false); onSaved?.(); return true
  }
  const apply = async (ids: string[]) => {
    const result = await applyAIPreset(selected, ids)
    setMessage(result.message || '应用结束'); setResults(result.data?.results || [])
    if (result.data?.results.some(row => row.account_id === accountId && row.success)) { await reload(); onSaved?.() }
  }
  return <section className="space-y-5 rounded-2xl bg-white p-5 text-slate-800" aria-label="AI 配置与预设">
    <header className="flex items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-widest text-teal-700">AI / 账号独立配置</p><h2 className="mt-1 text-xl font-semibold">{accountId}</h2><p className="mt-1 text-xs text-slate-500">测试与真实回复共用协议；超时后不自动重复生成。</p></div>{onClose && <button className={button} onClick={onClose} disabled={busy}>关闭</button>}</header>
    <p role="status" className="text-sm text-teal-800">{message}</p>
    {!settings ? <p>正在加载 AI 配置…</p> : <fieldset disabled={busy} className="space-y-5">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={settings.ai_enabled} onChange={event => update({ ai_enabled: event.target.checked })} />启用 AI 回复<span className="ml-auto text-xs text-slate-500">配置版本 {settings.config_version}{dirty ? ' · 有未保存更改' : ' · 已保存'}</span></label>
      <AIProtocolFields settings={settings} onChange={update} />
      <div className="flex flex-wrap items-center gap-3"><label className="text-xs text-slate-600"><input type="checkbox" checked={clearKey} onChange={event => { setClearKey(event.target.checked); setDirty(true) }} /> 明确清除密钥（须先停用 AI）</label><button className={button} onClick={() => run(async () => { const result = await loadAIModels(accountId, settings); setModels(result.data?.models || []); setMessage(result.message || '模型列表已更新') })}>获取模型列表</button>{models.length > 0 && <select aria-label="选择模型" className={input + ' !w-auto'} value={settings.model_name} onChange={event => update({ model_name: event.target.value })}><option value={settings.model_name}>{settings.model_name}</option>{models.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>}</div>
      <label className="block space-y-1 text-sm">提示词（文本或 default / price / tech JSON）<textarea className={input} rows={4} aria-label="提示词" value={settings.custom_prompts} onChange={event => update({ custom_prompts: event.target.value })} /></label>
      <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm">议价、时段与人工暂停</summary><div className="mt-3 grid gap-3 sm:grid-cols-3">{(['max_discount_percent', 'max_discount_amount', 'max_bargain_rounds', 'manual_reply_ai_pause_minutes'] as const).map((key, i) => <label key={key} className="text-sm">{['最大优惠 %', '最大优惠金额', '议价轮数上限', '人工回复暂停分钟'][i]}<input type="number" min={0} className={input} value={settings[key]} onChange={event => update({ [key]: Number(event.target.value) })} /></label>)}{(['ai_time_range_start', 'ai_time_range_end'] as const).map((key, i) => <label key={key} className="text-sm">{i ? '结束时间' : '开始时间'}<input type="time" className={input} value={settings[key]} onChange={event => update({ [key]: event.target.value })} /></label>)}</div><label className="mt-3 block text-sm"><input type="checkbox" checked={settings.manual_reply_ai_pause_enabled} onChange={event => update({ manual_reply_ai_pause_enabled: event.target.checked })} /> 人工回复后暂停 AI</label></details>
      <div className="flex flex-wrap gap-2"><button className={primary} onClick={() => run(async () => { await save(); setMessage('配置已保存') })}>保存配置</button><button className={button} onClick={() => run(async () => { if (await save()) { setProbe(null); const result = await probeAISettings(accountId); setProbe(result.data || { stage: 'internal' }); setMessage(result.message || '测试结束') } })}>保存并测试</button></div>
      {probe && <div className="rounded-xl border border-teal-200 bg-teal-50 p-3 text-sm" role="status"><strong>{stages[probe.stage] || probe.stage}</strong>{probe.status_code && <span> · HTTP {probe.status_code}</span>}{probe.reply && <p className="mt-2 whitespace-pre-wrap">{probe.reply}</p>}<p className="mt-2 break-all text-xs text-slate-500">版本 {probe.config_version ?? '—'} · 调用 {probe.call_id || '—'}{probe.provider_code ? ` · ${probe.provider_code}` : ''}</p></div>}
      <section className="space-y-3 border-t pt-5"><h3 className="font-semibold">我的预设</h3><p className="text-xs text-slate-500">应用时复制完整配置。删除预设不影响已应用的账号。</p><div className="grid gap-3 sm:grid-cols-2"><select className={input} aria-label="选择预设" value={selected} onChange={event => { setSelected(event.target.value); setPresetName(presets.find(row => row.id === event.target.value)?.name || '') }}><option value="">选择预设</option>{presets.map(row => <option key={row.id} value={row.id}>{row.name} · {row.settings.provider_type}</option>)}</select><input className={input} aria-label="预设名称" placeholder="预设名称" value={presetName} onChange={event => setPresetName(event.target.value)} /></div>
        <div className="flex flex-wrap gap-2"><button className={button} disabled={!presetName.trim()} onClick={() => run(async () => { const result = await createAIPreset(presetName, { ...settings, clear_api_key: clearKey }, accountId); if (!result.success) throw new Error(result.message); await refreshPresets(); setSelected(result.data?.id || ''); setMessage('当前配置已另存为预设') })}>另存新预设</button><button className={button} disabled={!selected || !presetName.trim()} onClick={() => run(async () => { const result = await updateAIPreset(selected, presetName); if (!result.success) throw new Error(result.message); await refreshPresets(); setMessage('预设已重命名') })}>重命名</button><button className={button} disabled={!selected} onClick={() => run(async () => { const result = await updateAIPreset(selected, presetName, { ...settings, clear_api_key: clearKey }); if (!result.success) throw new Error(result.message); await refreshPresets(); setMessage('已用当前表单更新预设；占位密钥保留预设原密钥') })}>更新预设配置</button><button className={button} disabled={!selected} onClick={() => run(async () => { const result = await deleteAIPreset(selected); if (!result.success) throw new Error(result.message); setSelected(''); await refreshPresets(); setMessage(result.message || '预设已删除') })}>删除预设</button><button className={button} disabled={!selected} onClick={() => run(() => apply([accountId]))}>应用到当前账号</button></div>
        <div className="flex flex-wrap gap-3">{(accounts.length ? accounts : [{ id: accountId }]).map(account => <label className="text-sm" key={account.id}><input type="checkbox" checked={targets.includes(account.id)} onChange={event => setTargets(current => event.target.checked ? [...current, account.id] : current.filter(value => value !== account.id))} /> {account.label || account.id}</label>)}</div><button className={primary} disabled={!selected || !targets.length} onClick={() => run(() => apply(targets))}>批量应用（{targets.length}）</button>
        {!!results.length && <ul className="space-y-1 text-sm" aria-label="逐账号应用结果">{results.map(row => <li key={row.account_id} className={row.success ? 'text-teal-700' : 'text-red-700'}>{row.account_id} · {row.success ? '已应用' : '失败'}{row.message ? `：${row.message}` : ''}</li>)}</ul>}
      </section>
      <BargainingDiagnostics accountId={accountId} />
    </fieldset>}
  </section>
}
