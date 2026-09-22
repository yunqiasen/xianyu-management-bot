import { useEffect, useState } from 'react'
import * as api from '@/api/replyControls'
import { useUIStore } from '@/store/uiStore'

const blankExclusive: api.ExclusiveReply = { item_id: '', content: '', image_url: '', enabled: true, version: 0 }
const blankFilter: api.AdvancedFilter = { all_accounts: false, pause_minutes: null, pattern: '', match_mode: 'contains', source: 'user', item_id: '', actions: ['skip_reply'], enabled: true, version: 0 }
const actionLabels: Record<string, string> = { notify: '通知', skip_ai: '跳过AI', skip_reply: '跳过回复', pause: '暂停会话', skip_notify: '抑制通知' }

export default function ReplyControls({ accountId }: { accountId: string }) {
  const { addToast } = useUIStore()
  const [policy, setPolicy] = useState<api.ReplyPolicy | null>(null)
  const [rules, setRules] = useState<api.ExclusiveReply[]>([])
  const [filters, setFilters] = useState<api.AdvancedFilter[]>([])
  const [images, setImages] = useState<api.ReplyImage[]>([])
  const [rule, setRule] = useState(blankExclusive)
  const [filter, setFilter] = useState(blankFilter)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = async () => {
    const [p, r, f, i] = await Promise.all([api.getReplyPolicy(accountId), api.getExclusiveReplies(accountId), api.getAdvancedFilters(accountId), api.getReplyImages(accountId)])
    setPolicy(p); setRules(r); setFilters(f); setImages(i)
  }
  useEffect(() => {
    let current = true
    setPolicy(null); setError(''); setRule(blankExclusive); setFilter(blankFilter)
    Promise.all([api.getReplyPolicy(accountId), api.getExclusiveReplies(accountId), api.getAdvancedFilters(accountId), api.getReplyImages(accountId)])
      .then(([p, r, f, i]) => { if (current) { setPolicy(p); setRules(r); setFilters(f); setImages(i) } })
      .catch(e => { if (current) setError(e.message || '配置加载失败') })
    return () => { current = false }
  }, [accountId])
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError('')
    try { await action(); await load() } catch (e) { setError(e instanceof Error ? e.message : '保存失败') } finally { setBusy(false) }
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(rules, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `exclusive-${accountId}.json`; link.click(); URL.revokeObjectURL(url)
  }
  return <section className="vben-card" aria-label="回复策略与规则">
    <div className="vben-card-body space-y-5">
      <div><h2 className="font-semibold">回复控制台</h2><p className="text-sm text-gray-500">账号 {accountId} · 规则按版本保存，冲突时刷新，不覆盖别人修改。</p></div>
      {error && <p role="alert" className="text-red-600 text-sm">{error}</p>}
      {!policy ? <p className="text-sm">{error ? '配置未加载' : '加载配置…'}</p> : <>
        <div className="flex flex-wrap gap-3 items-end border-b pb-4">
          <label className="text-sm">回复顺序<select className="input mt-1" value={policy.strategy} disabled={busy} onChange={e => setPolicy({ ...policy, strategy: e.target.value as api.ReplyPolicy['strategy'] })}>
            <option value="ai_first">新策略：专属 → 关键词 → AI → 默认</option><option value="legacy">迁移策略：专属 → 关键词 → 默认 → AI</option>
          </select></label>
          <label className="text-sm"><input type="checkbox" checked={policy.block_personal} onChange={e => setPolicy({ ...policy, block_personal: e.target.checked })} /> 个人黑名单拦截回复</label>
          <label className="text-sm"><input type="checkbox" checked={policy.block_platform} onChange={e => setPolicy({ ...policy, block_platform: e.target.checked })} /> 平台黑名单拦截回复</label>
          <button className="btn-ios-primary" disabled={busy} onClick={() => run(() => api.saveReplyPolicy(accountId, policy))}>保存策略</button>
          <p className="text-xs text-gray-500 w-full">这两个开关只影响回复；订单履约规则独立。当前版本 v{policy.version}。</p>
        </div>
        <details open><summary className="font-medium cursor-pointer">商品专属回复 <span className="text-gray-400">/ {rules.length}</span></summary>
          <p className="text-xs text-gray-500 my-2">专属优先于关键词，与商品默认兜底分开。文本和图片都留空 = 明确跳过，停止回复链。</p>
          <div className="grid sm:grid-cols-2 gap-3">
            <label className="text-sm">商品ID<input className="input" value={rule.item_id} onChange={e => setRule({ ...rule, item_id: e.target.value })} /></label>
            <label className="text-sm">回复图片<select className="input" value={rule.image_url} onChange={e => setRule({ ...rule, image_url: e.target.value })}><option value="">无图片</option>{images.map(i => <option key={i.id} value={i.url}>{i.width}×{i.height} · {i.id.slice(0, 8)}</option>)}</select></label>
            <label className="text-sm sm:col-span-2">回复正文<textarea className="input" value={rule.content} onChange={e => setRule({ ...rule, content: e.target.value })} /></label>
          </div>
          <div className="flex flex-wrap gap-2 my-3"><button className="btn-ios-primary" disabled={busy || !rule.item_id.trim()} onClick={() => run(async () => { await api.saveExclusiveReply(accountId, rule); setRule(blankExclusive) })}>{rule.id ? '保存修改' : '新增专属规则'}</button>
            <button className="btn-ios-secondary" onClick={() => setRule(blankExclusive)}>清空编辑</button><button className="btn-ios-secondary" onClick={download}>导出JSON</button>
            <label className="btn-ios-secondary">导入JSON<input type="file" accept=".json" className="hidden" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) run(async () => { const res = await api.importExclusiveReplies(accountId, JSON.parse(await file.text())); addToast({ type: res.data.errors.length ? 'warning' : 'success', message: `保存${res.data.saved}条；${res.data.errors.map(x => `第${x.row}行${x.message}`).join('；') || '无错误'}` }) }); e.target.value = '' }} /></label></div>
          <ul className="divide-y">{rules.map(r => <li key={r.id} className="py-2 flex gap-3 text-sm items-center"><span className="font-mono">{r.item_id}</span><span className="flex-1 truncate">{r.content || (r.image_url ? '[图片]' : '[明确跳过]')} · v{r.version}{!r.enabled && ' · 已停用'}</span><button onClick={() => setRule(r)}>编辑</button><button disabled={busy} onClick={() => run(() => api.saveExclusiveReply(accountId, { ...r, enabled: !r.enabled }))}>{r.enabled ? '停用' : '启用'}</button><button disabled={busy} onClick={() => run(() => api.deleteExclusiveReply(accountId, r))}>删除</button></li>)}</ul>
        </details>
        <details><summary className="font-medium cursor-pointer">高级过滤 <span className="text-gray-400">/ {filters.length}</span></summary>
          <div className="grid sm:grid-cols-3 gap-3 mt-3">
            <label className="text-sm">来源<select className="input" value={filter.source} onChange={e => setFilter({ ...filter, source: e.target.value as api.AdvancedFilter['source'] })}><option value="user">用户</option><option value="system">系统</option><option value="ai">AI出站</option><option value="all">全部</option></select></label>
            <label className="text-sm">匹配<select className="input" value={filter.match_mode} onChange={e => setFilter({ ...filter, match_mode: e.target.value as api.AdvancedFilter['match_mode'] })}><option value="contains">包含</option><option value="exact">精确</option><option value="regex">简单正则</option></select></label>
            <label className="text-sm">商品范围<input className="input" placeholder="留空为全部" value={filter.item_id} onChange={e => setFilter({ ...filter, item_id: e.target.value })} /></label>
            <label className="text-sm sm:col-span-3">过滤内容<input className="input" maxLength={256} value={filter.pattern} onChange={e => setFilter({ ...filter, pattern: e.target.value })} /></label>
          </div>
          <div className="flex flex-wrap gap-3 mt-3">
            <label className="text-sm"><input type="checkbox" checked={Boolean(filter.all_accounts)} onChange={e => setFilter({ ...filter, all_accounts: e.target.checked })} /> 本人全部账号（含今后新增）</label>
            {filter.actions.includes('pause') && <label className="text-sm">过滤暂停分钟数<input className="input" type="number" min={0} max={1440} placeholder="留空继承账号" value={filter.pause_minutes ?? ''} onChange={e => setFilter({ ...filter, pause_minutes: e.target.value === '' ? null : Number(e.target.value) })} /></label>}
          </div>
          <div className="flex flex-wrap gap-3 my-3">{Object.entries(actionLabels).map(([key, label]) => <label className="text-sm" key={key}><input type="checkbox" checked={filter.actions.includes(key)} onChange={e => setFilter({ ...filter, actions: e.target.checked ? [...filter.actions, key] : filter.actions.filter(a => a !== key) })} /> {label}</label>)}<button className="btn-ios-primary" disabled={busy || !filter.pattern || !filter.actions.length} onClick={() => run(async () => { await api.saveAdvancedFilter(accountId, filter); setFilter(blankFilter) })}>保存过滤</button></div>
          <p className="text-xs text-gray-500">只选“通知”不会拦截回复。正则最多256字符，不支持分组、分支和多个重复项。</p>
          <ul className="divide-y">{filters.map(f => <li key={f.id} className="py-2 flex gap-3 text-sm"><span className="flex-1 truncate">{f.all_accounts ? '全部账号' : '当前账号'} / {f.source} / {f.pattern} / {f.actions.map(a => actionLabels[a]).join('、')}{f.actions.includes('pause') && `（${f.pause_minutes ?? '继承账号'}分钟）`} / v{f.version}</span><button onClick={() => setFilter(f)}>编辑</button><button disabled={busy} onClick={() => run(() => api.saveAdvancedFilter(accountId, { ...f, enabled: !f.enabled }))}>{f.enabled ? '停用' : '启用'}</button><button disabled={busy} onClick={() => run(() => api.deleteAdvancedFilter(accountId, f))}>删除</button></li>)}</ul>
        </details>
        <details><summary className="font-medium cursor-pointer">回复图片 <span className="text-gray-400">/ {images.length}</span></summary>
          <label className="btn-ios-secondary my-3">上传图片<input className="hidden" type="file" accept="image/png,image/jpeg,image/gif,image/webp" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) run(() => api.uploadReplyImage(accountId, file)); e.target.value = '' }} /></label>
          <div className="flex flex-wrap gap-3">{images.map(i => <div className="border rounded p-2 w-32 text-xs" key={i.id}><img src={i.url} alt="回复资源预览" className="h-20 w-full object-contain" /><p>{i.width}×{i.height} · {Math.ceil(i.size / 1024)}KB</p><button disabled={busy} onClick={() => run(async () => { const refs = await api.imageReferences(accountId, i.id); if (refs.length) { addToast({ type: 'warning', message: `在用引用${refs.length}条，保留图片` }); return } await api.deleteReplyImage(accountId, i.id) })}>检查引用并删除</button></div>)}</div>
        </details>
      </>}
    </div>
  </section>
}
