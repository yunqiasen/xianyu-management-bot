import { useCallback, useEffect, useState } from 'react'
import { getAllCards, type CardData } from '@/api/cards'
import { createDeliveryRule, getDeliveryRules, updateDeliveryRule, type DeliveryRule, type DeliveryRuleInput } from '@/api/orderCommerce'

const blank = (account: string): DeliveryRuleInput => ({ account_id: account || null, card_id: 0, keyword: '', match_mode: 'contains', delivery_count: 1, enabled: false, priority: 0, description: '' })
export function DeliveryRulesPanel({ account }: { account: string }) {
  const [rows, setRows] = useState<DeliveryRule[]>([])
  const [cards, setCards] = useState<CardData[]>([])
  const [draft, setDraft] = useState<DeliveryRuleInput>(blank(account))
  const [editing, setEditing] = useState<DeliveryRule | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = useCallback(async () => setRows((await getDeliveryRules(account)).data), [account])
  useEffect(() => {
    let current = true
    setRows([]); setEditing(null); setDraft(blank(account)); setError('')
    Promise.all([getDeliveryRules(account), getAllCards()]).then(([rules, options]) => {
      if (current) { setRows(rules.data); setCards(options) }
    }).catch(() => { if (current) setError('发货规则读取失败') })
    return () => { current = false }
  }, [account])
  async function act(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action(); await load() }
    catch (e) { setError(e instanceof Error ? e.message : '规则保存失败，请刷新后核对版本') }
    finally { setBusy(false) }
  }
  return <section aria-label="标题发货规则" className="border rounded p-3 space-y-3">
    <h3 className="font-semibold">标题发货规则</h3>
    <p className="text-xs text-slate-500">按商品标题选择卡券；SKU仍需精确匹配。商品直接关联优先，标题规则按优先级匹配。保存不会立即发货。</p>
    {error && <p role="alert" className="text-red-600 text-sm">{error}</p>}
    <form className="space-y-3" onSubmit={event => { event.preventDefault(); void act(async () => {
      if (editing) await updateDeliveryRule(editing.id, { ...draft, expected_version: editing.version })
      else await createDeliveryRule(draft)
      setEditing(null); setDraft(blank(account))
    }) }}>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-sm">标题关键词<input className="input-ios w-full" required maxLength={255} value={draft.keyword} onChange={e => setDraft({ ...draft, keyword: e.target.value })} /></label>
        <label className="text-sm">使用卡券<select className="input-ios w-full" required value={draft.card_id || ''} onChange={e => setDraft({ ...draft, card_id: Number(e.target.value) })}><option value="">请选择卡券</option>{cards.map(card => <option key={card.id} value={card.id}>{card.name} · {card.type}{card.enabled === false ? '（已暂停）' : ''}</option>)}</select></label>
        <label className="text-sm">匹配方式<select className="input-ios w-full" value={draft.match_mode} onChange={e => setDraft({ ...draft, match_mode: e.target.value as DeliveryRuleInput['match_mode'] })}><option value="contains">标题包含关键词</option><option value="exact">标题完全相同</option><option value="legacy_contains">旧版双向包含</option></select></label>
        <label className="text-sm">规则账号范围<select className="input-ios w-full" value={draft.account_id || ''} onChange={e => setDraft({ ...draft, account_id: e.target.value || null })}><option value="">本人全部账号（含今后新增）</option>{account && <option value={account}>当前账号 {account}</option>}</select></label>
        <label className="text-sm">发放倍数<input className="input-ios w-full" type="number" min={1} max={1000} required value={draft.delivery_count} onChange={e => setDraft({ ...draft, delivery_count: Number(e.target.value) })} /></label>
        <label className="text-sm">规则优先级<input className="input-ios w-full" type="number" min={-1000} max={1000} required value={draft.priority} onChange={e => setDraft({ ...draft, priority: Number(e.target.value) })} /></label>
      </div>
      <label className="text-sm block">规则备注<input className="input-ios w-full" maxLength={1000} value={draft.description || ''} onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
      <div className="flex gap-3 items-center"><label className="text-sm"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} /> 规则启用</label><button className="btn-ios-primary" disabled={busy || !draft.card_id || !draft.keyword.trim()}>{editing ? '保存发货规则' : '新增发货规则'}</button>{editing && <button type="button" className="btn-ios-secondary" onClick={() => { setEditing(null); setDraft(blank(account)) }}>取消编辑</button>}</div>
    </form>
    <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th>关键词 / 范围</th><th>卡券 / 倍数</th><th>状态 / 使用次数</th><th>操作</th></tr></thead><tbody>{rows.map(row => <tr key={row.id} className="border-t"><td className="py-2">{row.keyword}<div className="text-xs text-slate-500">{row.account_id || '本人全部账号'} · 优先级 {row.priority}</div></td><td>{cards.find(c => c.id === row.card_id)?.name || `卡券 ${row.card_id}`} × {row.delivery_count}</td><td>{row.enabled ? '已启用' : '已停用'} · {row.delivery_times}<div className="text-xs text-slate-500">v{row.version}</div></td><td><div className="flex gap-2"><button disabled={busy} onClick={() => { setEditing(row); const { id: _id, version: _version, delivery_times: _times, ...input } = row; setDraft(input) }}>编辑</button><button disabled={busy} onClick={() => act(() => updateDeliveryRule(row.id, { enabled: !row.enabled, expected_version: row.version }))}>{row.enabled ? '停用' : '启用'}</button></div></td></tr>)}</tbody></table></div>
    {!rows.length && <p className="text-xs text-slate-500">暂无标题规则，已有商品关联继续生效。</p>}
  </section>
}
