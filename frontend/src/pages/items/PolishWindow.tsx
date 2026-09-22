import { useEffect, useState } from 'react'
import { get, put, post } from '@/utils/request'
import { useUIStore } from '@/store/uiStore'

type WindowData = { timezone_name?: string; start?: string; end?: string; next_run_at?: string; status?: string; enabled?: boolean; last_status?: string; randomize?: boolean; planned_at?: number }
export function PolishWindow({ accountId }: { accountId: string }) {
  const { addToast } = useUIStore()
  const [data, setData] = useState<WindowData>({ timezone_name: 'Asia/Shanghai', start: '09:00', end: '10:00' })
  const [history, setHistory] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let current = true
    setHistory([])
    setData({ timezone_name: 'Asia/Shanghai', start: '09:00', end: '10:00' })
    get<{ data: WindowData }>(`/api/v1/items/polish-window/${encodeURIComponent(accountId)}`)
      .then(r => { if (current) setData(d => ({ ...d, ...r.data })) })
      .catch(() => { if (current) addToast({ type: 'error', message: '擦亮时间窗读取失败' }) })
    return () => { current = false }
  }, [accountId, addToast])
  async function save() {
    setBusy(true)
    try {
      await put(`/api/v1/items/polish-window/${encodeURIComponent(accountId)}`, { timezone_name: data.timezone_name, start: data.start, end: data.end, randomize: Boolean(data.randomize) })
      addToast({ type: 'success', message: '时间窗已保存，原擦亮开关保持不变' })
    } catch { addToast({ type: 'error', message: '时间窗保存失败' }) }
    finally { setBusy(false) }
  }
  async function loadHistory() {
    try { setHistory((await get<{data:any[]}>(`/api/v1/items/polish/${encodeURIComponent(accountId)}/history`)).data) }
    catch { addToast({type:'error',message:'擦亮记录读取失败'}) }
  }
  async function manual() {
    setBusy(true)
    try {
      await post(`/api/v1/items/polish/${encodeURIComponent(accountId)}`)
      await loadHistory()
    } catch { addToast({type:'warning',message:'执行未确认，请先查看擦亮记录'}) }
    finally { setBusy(false) }
  }
  return <details className="vben-card p-4"><summary>账号擦亮时间窗（{data.enabled ? '擦亮已启用' : '擦亮未启用'}）</summary>
    <div className="flex flex-wrap gap-3 mt-3">
      <input aria-label="账号时区" className="input-ios" value={data.timezone_name} onChange={e => setData({ ...data, timezone_name: e.target.value })} />
      <input aria-label="开始时间" className="input-ios" type="time" value={data.start} onChange={e => setData({ ...data, start: e.target.value })} />
      <input aria-label="结束时间" className="input-ios" type="time" value={data.end} onChange={e => setData({ ...data, end: e.target.value })} />
      <label><input type="checkbox" checked={Boolean(data.randomize)} onChange={e => setData({ ...data, randomize: e.target.checked })} /> 窗口内随机错开</label>
      <button className="btn-ios-primary" disabled={busy} onClick={save}>保存时间窗</button>
      <button className="btn-ios-primary" disabled={busy} onClick={manual}>立即擦亮</button>
      <button className="btn-ios-secondary" onClick={loadHistory}>查看执行记录</button>
    </div><p className="text-sm mt-2">支持跨午夜；错过时间窗不补跑。{data.next_run_at && `下次窗口：${data.next_run_at}`}</p>
    {data.planned_at && <p className="text-sm">本周期计划：{new Date(data.planned_at * 1000).toLocaleString()}（重启保留）</p>}
    {data.last_status && <p className="text-sm">周期状态：{data.last_status}</p>}
    {history.map(row => <details key={row.id} className="text-sm mt-2"><summary>{row.created_at} · {row.source==='manual'?'手工':'定时'} · {row.status}</summary>
      <pre className="whitespace-pre-wrap">{JSON.stringify(row.result,null,2)}</pre></details>)}
  </details>
}
