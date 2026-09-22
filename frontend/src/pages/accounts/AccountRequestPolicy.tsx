import { useCallback, useEffect, useState } from 'react'
import {
  announceConfigurationChange, applyAccountConfiguration, readAccountRequestPolicy, saveAccountRequestPolicy,
  type AccountRequestPolicyState, type RequestPolicyValues,
} from '@/api/account-runtime'

export function AccountRequestPolicy({ accountId }: { accountId: string }) {
  const [policy, setPolicy] = useState<AccountRequestPolicyState>()
  const [minimum, setMinimum] = useState('10')
  const [maximum, setMaximum] = useState('')
  const [perMinute, setPerMinute] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const load = useCallback(async () => {
    const response = await readAccountRequestPolicy(accountId)
    if (!response.success || !response.data) throw new Error(response.message || '频率读取失败')
    setPolicy(response.data)
    setMinimum(String(response.data.values.min_interval_seconds ?? 10))
    setMaximum(String(response.data.values.max_interval_seconds ?? ''))
    setPerMinute(String(response.data.values.requests_per_minute ?? ''))
  }, [accountId])
  useEffect(() => {
    const refresh = (event: Event) => {
      if ((event as CustomEvent<string>).detail === accountId) void load().catch(() => setMessage('频率读取失败'))
    }
    void load().catch(() => setMessage('频率读取失败'))
    window.addEventListener('account-configuration-changed', refresh)
    return () => window.removeEventListener('account-configuration-changed', refresh)
  }, [accountId, load])
  const save = async () => {
    if (!policy) return
    const values: RequestPolicyValues = { min_interval_seconds: Number(minimum) }
    if (maximum.trim()) values.max_interval_seconds = Number(maximum)
    if (perMinute.trim()) values.requests_per_minute = Number(perMinute)
    if (Object.values(values).some(value => !Number.isFinite(value) || value <= 0)) {
      setMessage('间隔和频率需大于 0'); return
    }
    setBusy(true)
    try {
      const result = await saveAccountRequestPolicy(accountId, policy.config_version, values)
      if (!result.success || !result.data) throw new Error(result.message || '保存失败')
      setPolicy(result.data)
      setMessage('频率已保存，请应用待生效配置')
      announceConfigurationChange(accountId)
    } catch { setMessage('保存失败，请刷新配置后重试') }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!policy) return
    setBusy(true)
    try {
      const result = await applyAccountConfiguration(accountId, policy.config_version)
      if (!result.success || !result.data) throw new Error(result.message || '应用失败')
      setMessage(result.data.complete ? '三个服务已应用此版本' : `配置部分应用，待补齐：${result.data.pending_consumers.join('、')}`)
      await load()
      announceConfigurationChange(accountId)
    } catch { setMessage('配置应用失败，请刷新状态后重试') }
    finally { setBusy(false) }
  }
  const inputClass = 'mt-1 w-full rounded border border-slate-300 px-2 py-1 text-slate-900'
  return <section className="mt-3 space-y-2 border-t border-slate-200 pt-3" aria-label="账号业务频率">
    <h4 className="font-medium">业务频率与配置应用</h4>
    {policy && <p>{policy.configured ? `配置间隔：${policy.effective_interval_seconds} 秒` : '未设置业务频率，业务请求保持暂停'} · v{policy.config_version}</p>}
    <label className="block">最小请求间隔（秒）<input className={inputClass} type="number" min="0.1" step="any" value={minimum} onChange={e => setMinimum(e.target.value)} /></label>
    <label className="block">较慢间隔（秒，可留空）<input className={inputClass} type="number" min="0.1" step="any" value={maximum} onChange={e => setMaximum(e.target.value)} /></label>
    <label className="block">每分钟请求上限（可留空）<input className={inputClass} type="number" min="0.1" step="any" value={perMinute} onChange={e => setPerMinute(e.target.value)} /></label>
    <p>各限制取较慢值；客服、搜索和运营任务共用。应用配置不代表登录成功。</p>
    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={busy || !policy} className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50" onClick={() => void save()}>保存频率</button>
      <button type="button" disabled={busy || !policy?.pending_consumers.length} className="rounded border px-3 py-1.5 disabled:opacity-50" onClick={() => void apply()}>应用待生效配置</button>
      <button type="button" disabled={busy} className="text-blue-600" onClick={() => void load().catch(() => setMessage('刷新失败'))}>刷新配置</button>
    </div>
    {policy && <p>待应用：{policy.pending_consumers.join('、') || '无'}</p>}
    {message && <p role="status">{message}</p>}
  </section>
}
