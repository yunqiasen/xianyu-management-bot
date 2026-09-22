import { AccountTypedSettings } from './AccountTypedSettings'
import { AccountRequestPolicy } from './AccountRequestPolicy'
import { useCallback, useEffect, useState } from 'react'
import { readAccountRuntime, submitCredentialJob, cancelCredentialJob, clearSavedPassword, type AccountRuntime } from '@/api/account-runtime'

const states: Record<string, string> = {
  ready: '业务可用', unchecked: '凭据待检查', recovering: '恢复中', verification_required: '待人工验证',
  proxy_error: '代理异常', cooldown: '限流等待', paused: '失败暂停', disabled: '手动停用', config_pending: '配置待应用',
}
const jobs: Record<string, string> = { processing: '处理中', verified: '已验证', invalid: '验证失败', cancelled: '已取消', expired: '已超时', superseded: '旧版本失效' }
const terminal = new Set(['verified', 'invalid', 'cancelled', 'expired', 'superseded'])

export function AccountRuntimePanel({ accountId }: { accountId: string }) {
  const [open, setOpen] = useState(false)
  const [runtime, setRuntime] = useState<AccountRuntime>()
  const [cookie, setCookie] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try {
      const result = await readAccountRuntime(accountId)
      if (result.success && result.data) setRuntime(result.data)
      else setMessage(result.message || '状态读取失败')
    } catch { setMessage('状态读取失败') }
  }, [accountId])
  useEffect(() => {
    if (!open) return
    void load()
    const timer = window.setInterval(() => void load(), 4000)
    return () => window.clearInterval(timer)
  }, [open, load])
  const submit = async () => {
    if (!runtime || !cookie.trim()) return
    setBusy(true)
    try {
      const result = await submitCredentialJob(accountId, cookie, runtime.credential_version)
      setMessage(result.message || '任务已提交')
      if (result.success) setCookie('')
      await load()
    } catch { setMessage('提交失败，请刷新状态后重试') }
    finally { setBusy(false) }
  }
  return <div className="mt-1 text-xs font-normal text-slate-600 dark:text-slate-300">
    <button type="button" className="text-blue-600 underline" onClick={() => setOpen(!open)} aria-expanded={open}>运行状态与凭据维护</button>
    {open && <section className="mt-2 min-w-72 max-w-md rounded border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800" aria-label={`${accountId} 运行状态`}>
      {runtime ? <>
        <p className="font-medium">{runtime.enabled ? (states[runtime.business_state] || runtime.business_state) : '手动停用'} · 连接：{runtime.connection_state === 'connected' ? '已连接' : '未连接'}</p>
        <p className="mt-1">凭据 v{runtime.credential_version} · 配置 v{runtime.config_version} · 执行代次 {runtime.generation}</p>
        <p>恢复预算：{runtime.recovery_attempts}/2；待应用：{runtime.pending_consumers.join('、') || '无'}</p>
        {runtime.reason && <p>原因：{runtime.reason}</p>}
        {runtime.last_success_at && <p>最近成功：{new Date(runtime.last_success_at * 1000).toLocaleString()}</p>}
        {runtime.next_retry_at > 0 && <p>下次尝试：{new Date(runtime.next_retry_at * 1000).toLocaleString()}</p>}
        {runtime.jobs.map(job => <div key={job.id} className="mt-2 flex items-center justify-between gap-2">
          <span>{job.kind}：{jobs[job.status] || job.status}</span>
          {!terminal.has(job.status) && <button type="button" className="text-red-600" onClick={async () => {
            try { await cancelCredentialJob(accountId, job.id); await load() } catch { setMessage('取消失败') }
          }}>取消任务</button>}
        </div>)}
        <label className="mt-3 block">导入新 Cookie（仅更新凭据）
          <textarea className="mt-1 w-full rounded border p-2 text-slate-900" rows={2} value={cookie} autoComplete="off" spellCheck={false} onChange={event => setCookie(event.target.value)} />
        </label>
        <button type="button" className="mt-2 rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50" disabled={busy || !cookie.trim()} onClick={() => void submit()}>提交检查</button>
        {runtime.has_password && <button type="button" className="ml-3 text-red-600" onClick={async () => {
          if (!window.confirm('仅清除已保存登录密码？其他账号配置保留。')) return
          try { await clearSavedPassword(accountId); await load() } catch { setMessage('清除失败') }
        }}>清除已保存密码</button>}
        <p className="mt-2">提交、凭据验证、业务可用是不同状态；凭据维护保留手动停用。</p>
        <AccountRequestPolicy accountId={accountId} />
        <AccountTypedSettings accountId={accountId} />
      </> : <p>读取状态中…</p>}
      {message && <p className="mt-2" role="status">{message}</p>}
    </section>}
  </div>
}
