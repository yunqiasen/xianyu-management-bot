import { useEffect, useState } from 'react'
import { loadBargainingDiagnostics } from '@/api/aiReply'
import type { BargainingDiagnosticsData } from '@/types/aiReply'

export function BargainingDiagnosticsView({ data, onRefresh }: { data: BargainingDiagnosticsData; onRefresh: () => void }) {
  if (!data.candidate_enabled) return null
  return <section className="space-y-3 rounded-xl border border-amber-200 bg-amber-50/40 p-4" aria-label="议价与历史候选诊断">
    <div className="flex items-center justify-between gap-3"><h3 className="font-semibold">议价与历史 · 候选诊断</h3><button type="button" className="rounded-lg border px-3 py-1 text-sm hover:bg-white" onClick={onRefresh}>刷新诊断</button></div>
    <p className="text-xs text-slate-600">仅展示实际回复记录；候选开关不代表 DEV43 覆盖验收或发布通过。</p>
    {!data.records.length && <p className="text-sm text-slate-500">暂无候选回复记录</p>}
    <ul className="space-y-3">{data.records.map(row => <li key={row.log_id} className="rounded-lg border border-slate-200 bg-white p-3 text-sm">
      <p className="break-all font-medium">会话 {row.chat_id} · 已确认议价 {row.bargaining.count} 次</p>
      <p className="mt-1 text-slate-600">{row.bargaining.is_bargaining ? '本次为议价' : '本次为询价或普通咨询'}{row.reason === 'bargain_limit' ? ' · 已达议价上限' : ''}</p>
      {!!row.bargaining.missing_event_ids && <p className="text-amber-800">有消息缺少事件身份，未计次</p>}
      {row.history_window && <><p className="mt-1">采用 {row.history_window.selected_messages ?? 0} 条 · 裁剪 {row.history_window.trimmed_messages ?? 0} 条 · 人工消息 {row.history_window.manual_messages ?? 0} 条</p><p className="mt-1 break-all font-mono text-xs text-slate-500">{row.history_window.first_event_id || '—'} → {row.history_window.last_event_id || '—'}</p>{row.history_window.reason === 'latest_turn_over_budget' && <p className="text-amber-800">最新完整往来超出预算，本次未调用模型</p>}</>}
    </li>)}</ul>
  </section>
}

export default function BargainingDiagnostics({ accountId }: { accountId: string }) {
  const [data, setData] = useState<BargainingDiagnosticsData | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    setData(null); setError('')
    loadBargainingDiagnostics(accountId).then(result => {
      if (!active) return
      if (result.success && result.data) setData(result.data)
      else setError('议价诊断读取失败')
    }).catch(() => { if (active) setError('议价诊断读取失败') })
    return () => { active = false }
  }, [accountId, revision])
  if (error) return <p className="text-sm text-amber-800" role="status">{error}</p>
  return data ? <BargainingDiagnosticsView data={data} onRefresh={() => setRevision(value => value + 1)} /> : null
}
