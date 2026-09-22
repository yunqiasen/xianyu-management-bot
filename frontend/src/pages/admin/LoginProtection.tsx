import { useEffect, useState } from 'react'
import { get, post, put } from '@/utils/request'

interface Row { dimension: string; subject: string; failures: number; locked: boolean; locked_until: number }
interface Settings { window_seconds: number; lock_seconds: number; username_threshold: number; ip_threshold: number; version: number }
type SettingKey = Exclude<keyof Settings, 'version'>
const fields: { key: SettingKey; label: string; min: number; max: number }[] = [
  { key: 'window_seconds', label: '统计窗口（秒）', min: 60, max: 86400 },
  { key: 'lock_seconds', label: '锁定时间（秒）', min: 60, max: 86400 },
  { key: 'username_threshold', label: '用户名失败阈值', min: 1, max: 100 },
  { key: 'ip_threshold', label: 'IP失败阈值', min: 1, max: 1000 },
]
const configPath = '/api/v1/admin/login-protection/config'
const toDraft = (value: Settings) => Object.fromEntries(fields.map(f => [f.key, String(value[f.key])])) as Record<SettingKey, string>

export function LoginProtection() {
  const [rows, setRows] = useState<Row[]>([])
  const [config, setConfig] = useState<Settings | null>(null)
  const [draft, setDraft] = useState<Record<SettingKey, string> | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(0)
  const loadRows = async (offset = page) => {
    const result = await get<{ data: Row[] }>(`/api/v1/admin/login-protection?limit=100&offset=${offset * 100}`)
    setRows(result.data)
  }
  const load = async () => {
    setBusy(true)
    try {
      const [result] = await Promise.all([get<{ data: Settings }>(configPath), loadRows()])
      setConfig(result.data); setDraft(toDraft(result.data)); setMessage('')
    } catch { setMessage('登录防护加载失败') } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [])
  const unlock = async (row: Row) => {
    setBusy(true)
    try {
      await post('/api/v1/admin/login-protection/unlock', { dimension: row.dimension, subject: row.subject })
      await loadRows(); setMessage('已解锁选中项，其他防护项保持原状')
    } catch { setMessage('解锁失败') } finally { setBusy(false) }
  }
  const save = async () => {
    if (!draft || !config) return
    const values = Object.fromEntries(fields.map(f => [f.key, Number(draft[f.key])])) as Omit<Settings, 'version'>
    if (fields.some(f => !draft[f.key].trim() || !Number.isInteger(values[f.key]) || values[f.key] < f.min || values[f.key] > f.max)) {
      setMessage('请按标注范围填写整数'); return
    }
    setBusy(true)
    try {
      const result = await put<{ data: Settings }>(configPath, { ...values, expected_version: config.version })
      setConfig(result.data); setDraft(toDraft(result.data)); setMessage('配置已保存；后续登录使用新规则，已有锁定需到期或定向解锁')
    } catch { setMessage('配置保存失败；若其他管理员已修改，请刷新后重试') } finally { setBusy(false) }
  }
  const paginate = async (next: number) => {
    setBusy(true)
    try { await loadRows(next); setPage(next) } catch { setMessage('登录防护加载失败') } finally { setBusy(false) }
  }
  return <section className="vben-card p-4 space-y-3" aria-label="后台登录防护">
    <div className="flex justify-between"><h2 className="font-semibold">后台登录防护</h2><button className="btn-ios-secondary" disabled={busy} onClick={load}>刷新</button></div>
    <p className="text-sm text-slate-500">用户名与IP独立统计；定向解锁仅影响选中项，与闲鱼账号保活次数分开。</p>
    {draft && config && <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">{fields.map(field => <label key={field.key} className="text-sm space-y-1">
        <span>{field.label}（{field.min}–{field.max}）</span>
        <input className="input-ios" type="number" aria-label={field.label} min={field.min} max={field.max} step={1} disabled={busy}
          value={draft[field.key]} onChange={e => setDraft({ ...draft, [field.key]: e.target.value })} />
      </label>)}</div>
      <p className="text-xs text-slate-500">当前版本 {config.version}：{config.window_seconds}秒内用户名{config.username_threshold}次 / IP {config.ip_threshold}次，锁定{config.lock_seconds}秒。</p>
      <button className="btn-ios-primary" disabled={busy} onClick={save}>保存防护配置</button>
    </div>}
    {message && <p role="status">{message}</p>}
    <div className="overflow-x-auto"><table className="table-ios"><thead><tr><th>维度</th><th>对象</th><th>失败次数</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.map(row => <tr key={row.dimension + row.subject}>
      <td>{row.dimension}</td><td>{row.subject}</td><td>{row.failures}</td><td>{row.locked ? '锁定至 ' + new Date(row.locked_until * 1000).toLocaleString() : '未锁定'}</td>
      <td><button className="btn-ios-secondary" disabled={busy || (!row.locked && !row.failures)} onClick={() => unlock(row)}>定向解锁</button></td>
    </tr>)}</tbody></table>{!busy && !rows.length && <p>暂无登录失败记录</p>}</div>
    <div className="flex gap-2"><button className="btn-ios-secondary" disabled={busy || page === 0} onClick={() => paginate(page - 1)}>上一页</button><span>第 {page + 1} 页</span><button className="btn-ios-secondary" disabled={busy || rows.length < 100} onClick={() => paginate(page + 1)}>下一页</button></div>
  </section>
}
