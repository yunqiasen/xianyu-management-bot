import { controlPublishBatch, reconcilePublish, getPublishEvidence } from '@/api/productPublish'

type CommonProps = { onChanged: (message: string) => void }
export function ProductBatchControls({ batchId, pending, failed, onChanged }: CommonProps & { batchId: string; pending: number; failed: number }) {
  async function act(action: 'cancel' | 'retry-failed' | 'resume') {
    try { const r = await controlPublishBatch(batchId, action); onChanged(r.message || '操作已记录') }
    catch { onChanged('操作未确认，请刷新批次状态') }
  }
  return <div className="flex flex-wrap gap-2 my-3" aria-label="批次操作">
    <button className="btn-ios-danger" disabled={!pending} onClick={() => act('cancel')}>取消未发项</button>
    <button className="btn-ios-primary" disabled={!failed} onClick={() => act('retry-failed')}>仅重试明确失败项</button>
    <button className="btn-ios-secondary" disabled={!pending} onClick={() => act('resume')}>继续未发项</button>
    <p className="text-sm text-slate-500">成功与待核实项不重发；中断批次需手动继续。</p>
  </div>
}

export function PublishReconcileControls({ logId, status, onChanged, onEvidence }: CommonProps & { logId: number; status: string; onEvidence: (text: string) => void }) {
  async function reconcile(outcome: 'published' | 'not_published') {
    const itemId = outcome === 'published' ? window.prompt('已同步且属于该账号的平台商品ID') : undefined
    if (outcome === 'published' && !itemId?.trim()) return
    const evidence = window.prompt('填写核对依据：商品列表、发布时间、截图编号等（至少5字）')
    if (!evidence || evidence.trim().length < 5) return
    try {
      const r = await reconcilePublish(logId, { outcome, item_id: itemId || undefined, evidence: evidence.trim() })
      onChanged(r.message || '核对结果已保存')
    } catch { onChanged('核对未保存，请刷新记录') }
  }
  return <div className="flex flex-wrap gap-2 text-xs mt-2">
    {['unknown', 'publishing'].includes(status) && <>
      <button className="text-blue-600" onClick={() => reconcile('published')}>核对已发布</button>
      <button className="text-amber-600" onClick={() => reconcile('not_published')}>核对未发布</button>
    </>}
    <button className="text-slate-500" onClick={async () => {
      try { const r = await getPublishEvidence(logId); onEvidence(JSON.stringify(r.data, null, 2)) }
      catch { onChanged('操作证据读取失败') }
    }}>查看操作证据</button>
  </div>
}
