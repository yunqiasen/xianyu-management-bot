import { useCallback, useEffect, useRef, useState } from 'react'
import { displaySpec, saveSupplierEvidence, advanceDeliveryIntent, querySupplier, getHistoryJobs, getDeliveryIntents, historyAction, confirmDeliveryIntent, resendDeliveryIntent, reconcileDeliveryIntent, getIntentContent, previewDeliveryRule, type HistoryJob, type DeliveryIntent, type RulePreview } from '@/api/orderCommerce'

import { DeliveryRulesPanel } from './DeliveryRulesPanel'

const labels: Record<string, string> = { reserved: '已预占', purchasing: '采购待核实', sending: '发送待核实', unknown: '待核实', confirmed: '已确认', not_sent: '明确未发送', not_required: '无需确认', pending: '待处理', confirming: '确认待核实', running: '同步中', partial: '部分完成', cancelled: '已取消', paused: '已暂停', completed: '完成' }
const label = (state: string) => labels[state] || state

type CommercePanelProps = { account: string; revision: number }

export function CommercePanel(props: CommercePanelProps) {
  // 账号变化时隔离整个操作界面，旧请求也只持有旧实例的状态。
  return <AccountCommercePanel key={props.account} {...props} />
}

function AccountCommercePanel({ account, revision }: CommercePanelProps) {
  const [jobs, setJobs] = useState<HistoryJob[]>([])
  const [intents, setIntents] = useState<DeliveryIntent[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [content, setContent] = useState<string | null>(null)
  const [rules, setRules] = useState<RulePreview[]>([])
  const [verify, setVerify] = useState<{ id: string; phase: 'content' | 'confirm'; result: 'confirmed' | 'not_sent' } | null>(null)
  const [evidence, setEvidence] = useState('')
  const [purchaseEvidence, setPurchaseEvidence] = useState<DeliveryIntent | null>(null)
  const [purchaseTexts, setPurchaseTexts] = useState('')
  const [previewOrder, setPreviewOrder] = useState('')
  const [resendReason, setResendReason] = useState('')
  const [resend, setResend] = useState<{ row: DeliveryIntent; requestId: string } | null>(null)
  const runVersion = useRef(0)
  const viewVersion = useRef(0)
  const refreshVersion = useRef(0)
  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current
    try {
      const [j, i] = await Promise.all([getHistoryJobs(account), getDeliveryIntents(account)])
      if (version === refreshVersion.current) { setJobs(j.data); setIntents(i.data) }
    } catch (error) { if (version === refreshVersion.current) throw error }
  }, [account])
  useEffect(() => {
    const version = viewVersion.current
    setContent(null); setRules([]); setVerify(null); setResend(null); setPurchaseEvidence(null); setPurchaseTexts('')
    refresh().catch(() => { if (version === viewVersion.current) setError('读取履约任务失败') })
    return () => { runVersion.current++; refreshVersion.current++; viewVersion.current++ }
  }, [refresh, revision])
  async function act(id: string, action: (current: () => boolean) => Promise<unknown>) {
    const version = viewVersion.current
    const current = () => version === viewVersion.current
    setBusy(id); setError('')
    try { await action(current); if (current()) await refresh() }
    catch { if (current()) setError('操作未完成，保留原阶段。刷新后核实结果。') }
    finally { if (current()) setBusy('') }
  }
  async function run(job: HistoryJob) {
    const version = ++runVersion.current
    await act(job.id, async () => {
      let row = (await historyAction(job.id, 'resume')).data
      while (version === runVersion.current && row.status === 'pending') {
        row = (await historyAction(job.id, 'step')).data
        if (version === runVersion.current) setJobs(current => current.map(j => j.id === row.id ? row : j))
      }
    })
  }
  return <section className="vben-card p-4 space-y-4" aria-label="订单历史和履约检查点">
    <div className="flex items-center justify-between gap-3"><div><h2 className="font-semibold">历史续跑 · 履约阶段</h2><p className="text-xs text-slate-500 mt-1">内容发送与平台发货分别确认，待核实期间保留库存。</p></div><button className="btn-ios-secondary" onClick={() => act('refresh', refresh)}>刷新任务</button></div>
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {jobs.map(job => <div key={job.id} className="border-l-2 border-slate-300 pl-3 text-sm flex flex-wrap items-center gap-3">
      <span className="font-mono text-xs break-all">{job.id}</span><span>{label(job.status)} · 下一页 {job.next_page}/{job.total_pages || '?'} · 已处理 {job.imported}</span>
      {job.status !== 'completed' && <><button disabled={busy === job.id} className="btn-ios-secondary" onClick={() => run(job)}>继续</button><button className="btn-ios-secondary" onClick={() => { runVersion.current++; void act(`cancel-${job.id}`, () => historyAction(job.id, 'cancel')) }}>取消</button></>}
    </div>)}
    {!jobs.length && <p className="text-xs text-slate-500">选择账号后，点击「获取闲鱼订单」建立历史任务。</p>}
    <div className="overflow-x-auto"><table className="w-full text-sm text-left"><thead><tr className="border-b text-slate-500"><th className="py-2">账号 / 订单</th><th>卡券 / 数量</th><th>内容发送</th><th>平台确认</th><th>操作</th></tr></thead><tbody>
      {intents.map(row => <tr key={row.id} className="border-b border-slate-100 dark:border-slate-800"><td className="py-3"><div className="text-xs text-slate-500">{row.account_id}</div>{row.order_no}<div className="font-mono text-xs">{row.operation_key === 'payment' || row.operation_key.startsWith('payment:') ? '首次履约' : '人工补发'}</div></td><td>{row.card_type} × {row.quantity}<div className="text-xs text-slate-500">{row.source?.card_name} · {displaySpec(row.source?.spec_name)} {displaySpec(row.source?.spec_value)}</div><div className="text-xs text-slate-500">来源配置 {row.source?.card_updated_at || "旧记录"} · 订单 {row.source?.order_quantity ?? row.quantity} 件</div><div className="text-xs text-slate-500">明细 {row.line_id || 'main'} · 金额 {row.source?.line_amount ?? '待核实'}{row.source?.rule_id && ` · 标题规则 v${row.source.rule_version}`}</div></td><td>{label(row.content_state)}<details className="text-xs"><summary>逐件结果 / 核实凭据</summary>{row.pieces?.filter(p => p.result !== "sending").map((p, i) => <div key={i}>{p.part} · {label(p.result)}</div>)}{row.evidence?.map((e, i) => <div key={i}>{e.phase} · {label(e.result)} {e.reference}</div>)}</details></td><td>{label(row.confirm_state)}<div className="text-xs text-slate-500">尝试 {row.confirm_attempts}/{row.confirm_attempt_limit}</div></td><td><div className="flex flex-wrap gap-2 py-2">
        {row.content_state === 'reserved' && <button className="btn-ios-secondary" disabled={!!busy || Date.now() / 1000 < row.not_before} onClick={() => act(row.id, () => advanceDeliveryIntent(row.id))}>继续履约</button>}
        {row.card_type === 'api' && ['unknown', 'purchasing'].includes(row.content_state) && <button className="btn-ios-secondary" disabled={!!busy} onClick={() => act(row.id, () => querySupplier(row.id))}>查询供应商</button>}
        {row.card_type === 'api' && ['unknown', 'purchasing'].includes(row.content_state) && <button className="btn-ios-secondary" disabled={!!busy} onClick={() => { setPurchaseEvidence(row); setPurchaseTexts(''); setEvidence('') }}>登记采购凭据</button>}
        {row.content_state === 'confirmed' && row.confirm_state === 'pending' && <button className="btn-ios-secondary" disabled={!!busy || row.confirm_attempts >= row.confirm_attempt_limit || Date.now() / 1000 < row.next_retry_at} onClick={() => act(row.id, () => confirmDeliveryIntent(row.id))}>仅补确认</button>}
        <button className="btn-ios-secondary" onClick={() => act(row.id, async current => { const r = await getIntentContent(row.id); if (current()) setContent(r.data ? [...r.data.texts, ...r.data.images].join('\n') : '供应商结果待核实') })}>查看内容</button>
        <button className="btn-ios-secondary" onClick={() => act(row.id, async current => { const result = await previewDeliveryRule(row.account_id, row.order_no); if (current()) setRules(result.data) })}>规则预览</button>
        {(['content', 'confirm'] as const).map(phase => ['unknown', 'sending', 'confirming'].includes(phase === 'content' ? row.content_state : row.confirm_state) && <button key={phase} className="btn-ios-secondary" onClick={() => { setEvidence(''); setVerify({ id: row.id, phase, result: 'confirmed' }) }}>核实{phase === 'content' ? '内容' : '确认'}</button>)}
        {row.content_state === 'confirmed' && <button className="btn-ios-secondary" onClick={() => { setResendReason(''); setResend({ row, requestId: crypto.randomUUID() }) }}>真正补发</button>}
      </div></td></tr>)}
    </tbody></table></div>
    {!intents.length && <p className="text-xs text-slate-500">暂无持久履约记录；旧订单的发货事实仍在订单详情中。</p>}
    {content !== null && <div className="rounded border p-3"><button className="btn-ios-secondary mb-2" onClick={() => setContent(null)}>隐藏内容</button><pre className="whitespace-pre-wrap break-all text-sm">{content}</pre></div>}
    <div className="flex flex-wrap gap-2"><input aria-label="规则预览订单号" className="input-ios" placeholder="订单号" value={previewOrder} onChange={e => setPreviewOrder(e.target.value)} /><button className="btn-ios-secondary" disabled={!account || !previewOrder} onClick={() => act('preview', async current => { const result = await previewDeliveryRule(account, previewOrder); if (current()) setRules(result.data) })}>预览匹配规则</button></div>
    {!!rules.length && <ul className="text-sm">{rules.map(r => <li key={`${r.line_id}:${r.rule_id || r.card_id}`}>{r.matched ? '✓' : '—'} 明细 {r.line_id} · {r.name} · {displaySpec(r.spec_name)} {displaySpec(r.spec_value)} · {r.reason} · 数量 {r.quantity}</li>)}</ul>}
    <DeliveryRulesPanel key={account} account={account} />
    {verify && <form className="border rounded p-3 flex flex-wrap gap-3" onSubmit={event => { event.preventDefault(); void act(verify.id, async current => { await reconcileDeliveryIntent(verify.id, verify.phase, verify.result, evidence); if (current()) setVerify(null) }) }}>
      <label>核实结果 <select className="input-ios" value={verify.result} onChange={event => setVerify({ ...verify, result: event.target.value as 'confirmed' | 'not_sent' })}><option value="confirmed">已取得成功证据</option><option value="not_sent">证据明确未发送</option></select></label>
      <label>凭据编号（勿填卡密）<input className="input-ios" required maxLength={120} pattern="[\w.:/\-]+" value={evidence} onChange={event => setEvidence(event.target.value)} /></label><button className="btn-ios-primary" disabled={!!busy}>保存核实结果</button><button type="button" className="btn-ios-secondary" onClick={() => setVerify(null)}>取消</button>
    </form>}
    {purchaseEvidence && <form className="border rounded p-3 space-y-3" onSubmit={event => { event.preventDefault(); void act(purchaseEvidence.id, async current => { await saveSupplierEvidence(purchaseEvidence.id, purchaseTexts.split('\n').filter(t => t.trim()), evidence); if (current()) { setPurchaseEvidence(null); setPurchaseTexts('') } }) }}>
      <p>仅登记已取得的 {purchaseEvidence.quantity} 份采购内容，不会再次购买；保存后点击继续履约。</p>
      <label className="block">供应商采购凭据编号<input className="input-ios" required maxLength={120} pattern="[\w.:/\-]+" value={evidence} onChange={event => setEvidence(event.target.value)} /></label>
      <label className="block">采购内容（每行一份，仅当前用户可见）<textarea className="input-ios w-full" required rows={4} value={purchaseTexts} onChange={event => setPurchaseTexts(event.target.value)} /></label>
      <button className="btn-ios-primary" disabled={!!busy}>确认登记已有采购</button><button type="button" className="btn-ios-secondary ml-2" onClick={() => { setPurchaseEvidence(null); setPurchaseTexts('') }}>取消</button>
    </form>}
    {resend && <div role="alertdialog" aria-label="确认再次出库" className="border border-amber-400 rounded p-3 space-y-3"><p>将重新出库并发送 {resend.row.quantity} 份内容，不是仅补确认。旧履约记录继续保留。</p><input className="input-ios" aria-label="补发原因" placeholder="补发原因（勿填卡密）" maxLength={200} value={resendReason} onChange={e => setResendReason(e.target.value)} /><button className="btn-ios-primary" disabled={!!busy || !resendReason.trim()} onClick={() => act(resend.row.id, async current => { await resendDeliveryIntent(resend.row.id, resend.requestId, resendReason); if (current()) setResend(null) })}>确认重新出库并发送</button><button className="btn-ios-secondary ml-2" onClick={() => setResend(null)}>取消</button></div>}
  </section>
}
