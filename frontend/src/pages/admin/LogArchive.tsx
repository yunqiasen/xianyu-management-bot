import { useState } from 'react'
import { get } from '@/utils/request'

interface Row {
  id: string
  source_table: string
  source_id: number | string
  payload: Record<string, unknown>
}

export function LogArchive() {
  const [rows, setRows] = useState<Row[]>([])
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [kind, setKind] = useState('runtime')

  const load = async (next = 0) => {
    setBusy(true)
    setError('')
    try {
      const source = kind === 'audit' ? '&source_table=xy_admin_audit' : ''
      const result = await get<{ data: Row[] }>(`/api/v1/admin/log-archive?limit=50&offset=${next}${source}`)
      setRows(result.data)
      setOffset(next)
      setLoaded(true)
    } catch {
      setError('归档加载失败')
    } finally {
      setBusy(false)
    }
  }

  return <details className="vben-card p-4">
    <summary>结构化日志归档 · 待核实记录保留在线</summary>
    <div className="space-y-2 mt-3">
      <div className="flex flex-wrap gap-2">
        <select aria-label="归档类别" className="input-ios" value={kind} disabled={busy}
          onChange={event => { setKind(event.target.value); setRows([]); setOffset(0); setLoaded(false); setError('') }}>
          <option value="runtime">运行日志（30天后归档）</option>
          <option value="audit">操作审计（180天后归档）</option>
        </select>
        <button type="button" className="btn-ios-secondary" disabled={busy} onClick={() => load()}>查询归档</button>
      </div>
      {error && <p role="alert">{error}</p>}
      {loaded && rows.length === 0 && <p>暂无归档记录</p>}
      {rows.map(row => <details key={row.id}>
        <summary>{row.source_table} #{row.source_id}</summary>
        <pre className="whitespace-pre-wrap break-all">{JSON.stringify(row.payload, null, 2)}</pre>
      </details>)}
      <div className="flex gap-2">
        <button type="button" className="btn-ios-secondary" disabled={busy || offset === 0}
          onClick={() => load(Math.max(0, offset - 50))}>上一页</button>
        <button type="button" className="btn-ios-secondary" disabled={busy || rows.length < 50}
          onClick={() => load(offset + 50)}>下一页</button>
      </div>
    </div>
  </details>
}
