import { useCallback, useEffect, useRef, useState } from 'react'
import { getMonitorReliability, saveMonitorRegion, type MonitorReliabilityStatus } from '@/api/listingMonitorReliability'
import type { ListingMonitorTask } from '@/api/listingMonitor'

const phases: Record<string, string> = {
  waiting_account: '所选账号暂停或恢复中，等待可用账号',
  pending: '待采集', collecting: '采集中', complete: '完整有效', empty: '有效空页', partial: '分页未齐',
  http_error: 'HTTP错误', platform_error: '平台业务错误', structure_error: '响应结构错误',
  submitted: '已提交，等待结果', unknown: '待核实，不自动重发', dispatch_failed: '账号通道失败',
  accepted: '渠道已受理', not_accepted: '明确未受理，按计划重试', exhausted: '重试用尽',
  sending: '投递中', cancelled: '已取消', queued: '已入通知队列', enqueue_failed: '入通知队列失败，待补齐',
}
const label = (phase: string) => phases[phase] || phase

export function MonitorReliabilityPanel({ tasks }: { tasks: ListingMonitorTask[] }) {
  const [selected, setSelected] = useState('')
  const taskId = tasks.some(t => String(t.id) === selected) ? Number(selected) : tasks[0]?.id
  const [data, setData] = useState<MonitorReliabilityStatus | null>(null)
  const [region, setRegion] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const sequence = useRef(0)
  const refresh = useCallback(async () => {
    const request = ++sequence.current
    if (!taskId) { setData(null); return }
    setBusy(true); setError('')
    try {
      const response = await getMonitorReliability(taskId)
      if (request !== sequence.current) return
      if (!response.success || !response.data) throw new Error(response.message || '读取监控状态失败')
      setData(response.data); setRegion(response.data.region)
    } catch (e) {
      if (request === sequence.current) { setData(null); setError(e instanceof Error ? e.message : '读取监控状态失败') }
    } finally { if (request === sequence.current) setBusy(false) }
  }, [taskId])
  useEffect(() => { setData(null); void refresh(); return () => { sequence.current += 1 } }, [refresh])
  const save = async () => {
    if (!taskId) return
    const current = sequence.current
    setBusy(true); setError('')
    try {
      const result = await saveMonitorRegion(taskId, region.trim())
      if (current !== sequence.current) return
      if (!result.success) throw new Error(result.message || '保存失败')
      await refresh()
    } catch (e) { if (current === sequence.current) setError(e instanceof Error ? e.message : '保存失败') }
    finally { if (current === sequence.current) setBusy(false) }
  }
  return <section className="card p-4 space-y-3" aria-label="监控可靠性">
    <div className="flex flex-wrap items-center gap-3">
      <h2 className="font-semibold text-slate-800 dark:text-slate-100">基线与通知</h2>
      <span className="text-xs text-amber-700 dark:text-amber-300">P5候选 · DEV43</span>
      <select aria-label="可靠性任务" className="input-ios max-w-xs" value={taskId ?? ''}
        onChange={e => setSelected(e.target.value)}>
        {!tasks.length && <option value="">暂无监控任务</option>}
        {tasks.map(t => <option key={t.id} value={t.id}>#{t.id} {t.keyword}</option>)}
      </select>
      <button type="button" className="btn-ios-secondary" disabled={busy || !taskId} onClick={() => void refresh()}>刷新可靠性</button>
    </div>
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {data && !data.enabled && <p className="text-sm text-slate-500">增强开关关闭，沿用原监控。DEV43覆盖验收门保留。</p>}
    {data?.enabled && <>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <strong className={data.baseline_ready ? 'text-emerald-700' : 'text-amber-700'}>
          {data.baseline_ready ? '完整基线已建立' : '基线未完成，不推新增'}
        </strong>
        <span>代次 {data.generation} · {label(data.phase)}</span>
        <span className="text-slate-500">发现事实与投递结果分别记录</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-sm" htmlFor="monitor-region">地区包含</label>
        <input id="monitor-region" aria-label="监控地区" className="input-ios max-w-xs" maxLength={120}
          value={region} onChange={e => setRegion(e.target.value)} placeholder="不限地区" />
        <button type="button" className="btn-ios-secondary" disabled={busy} onClick={() => void save()}>保存地区</button>
        <span className="text-xs text-slate-500">更改后重新建立完整基线，不推存量</span>
      </div>
      <ul className="flex flex-wrap gap-2 text-xs" aria-label="分页状态">
        {data.pages.map(p => <li key={p.page} className="rounded border border-slate-200 px-2 py-1 dark:border-slate-700">
          第{p.page}页 · {label(p.status)}{p.account_id ? ` · ${p.account_id}` : ''}
        </li>)}
      </ul>
      <div className="overflow-x-auto">
        <table className="table-ios w-full text-sm"><caption className="text-left text-xs text-slate-500 py-2">最近100条发现事件；无渠道不代表已送达</caption>
          <thead><tr><th>发现事实</th><th>价格</th><th>通知投递</th></tr></thead>
          <tbody>{data.events.map(e => <tr key={e.id}>
            <td>{e.summary}</td><td>{e.old_price ?? '—'} → {e.price}</td>
            <td>{label(e.enqueue_status)}{e.deliveries?.length ? e.deliveries.map(d =>
              <div key={d.channel_id}>渠道{d.channel_id}：{label(d.status)}（{d.attempts}次）</div>) : <div className="text-slate-500">尚无投递记录</div>}</td>
          </tr>)}{!data.events.length && <tr><td colSpan={3} className="text-slate-500">暂无发现事件</td></tr>}</tbody>
        </table>
      </div>
      <details className="text-sm"><summary className="cursor-pointer">当前查询的匹配商品（最多200条）：{data.items.length}</summary>
        <ul className="mt-2 space-y-1">{data.items.map(i => <li key={i.item_id}>#{i.item_id} {i.title} · ¥{i.price} · {i.area}</li>)}</ul>
      </details>
    </>}
  </section>
}
