import { useEffect, useState } from 'react'
import { getAccountDetails } from '@/api/accounts'
import AISettingsPanel from '@/pages/accounts/AISettingsPanel'

export default function AISettings() {
  const [accounts, setAccounts] = useState<{ id: string; label: string }[]>([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let current = true
    getAccountDetails().then(rows => {
      if (!current) return
      setAccounts(rows.map(row => ({ id: row.id, label: row.note || row.id })))
      setSelected(rows[0]?.id || '')
    }).catch(() => { if (current) setError('账号列表加载失败，请刷新重试') })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [])
  return <div className="max-w-5xl mx-auto p-4 space-y-5">
    <header><h1 className="text-xl font-semibold">AI 回复配置</h1><p className="text-sm text-slate-500 mt-1">协议配置、连通测试和预设批量应用。</p></header>
    <label className="block space-y-2 text-sm">选择账号<select aria-label="选择账号" className="w-full rounded-lg border p-2 dark:bg-slate-800" value={selected} onChange={event => setSelected(event.target.value)} disabled={loading || !accounts.length}>
      {!accounts.length && <option value="">{loading ? '正在加载…' : '请先在账号管理中添加账号'}</option>}
      {accounts.map(account => <option key={account.id} value={account.id}>{account.label} · {account.id}</option>)}
    </select></label>
    {error && <p role="alert" className="text-red-600">{error}</p>}
    {selected && <AISettingsPanel key={selected} accountId={selected} accounts={accounts} />}
  </div>
}
