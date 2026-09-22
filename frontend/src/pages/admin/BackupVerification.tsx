import { useEffect, useState } from 'react'
import { get, post, put, getApiErrorMessage } from '@/utils/request'

interface Evidence {
  id: string
  status: string
  sha256: string
  report: Record<string, unknown>
  protected: boolean
}

const STATUS_LABELS: Record<string, string> = {
  checked: '文件已校验', restoring: '恢复验证中', restored: '恢复验证通过', failed: '恢复验证失败',
}

export function BackupVerification({ logId }: { logId: number }) {
  const [rows, setRows] = useState<Evidence[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    let active = true
    setRows([])
    setMessage('')
    void get<{ data: Evidence[] }>(`/api/v1/db-backup-logs/${logId}/verifications`)
      .then(result => { if (active) setRows(result.data) })
      .catch(() => { if (active) setMessage('验证记录加载失败') })
    return () => { active = false }
  }, [logId])

  const verify = async (restore: boolean) => {
    setBusy(true)
    setMessage('')
    try {
      const result = await post<{ data: Evidence }>(`/api/v1/db-backup-logs/${logId}/verify?restore=${restore}`)
      setRows(previous => [result.data, ...previous.filter(row => row.id !== result.data.id)])
      setMessage(result.data.status === 'restored' ? '隔离恢复验证通过'
        : result.data.status === 'checked' ? '文件校验通过，尚未完成恢复' : '恢复验证失败，请核对隔离库')
    } catch (error) {
      setMessage(getApiErrorMessage(error, '验证失败'))
    } finally {
      setBusy(false)
    }
  }

  const protect = async (record: Evidence) => {
    setBusy(true)
    setMessage('')
    try {
      const result = await put<{ data: Evidence }>(
        `/api/v1/db-backup-logs/${logId}/verifications/${record.id}/protection`,
        { protected: !record.protected },
      )
      setRows(previous => previous.map(row => row.id === result.data.id ? result.data : row))
      setMessage(result.data.protected ? '已保护，自动清理会保留这份备份' : '已取消保护，后续按保留策略处理')
    } catch (error) {
      setMessage(getApiErrorMessage(error, '保护点设置失败'))
    } finally {
      setBusy(false)
    }
  }

  return <details className="text-xs mt-2">
    <summary>校验 / 隔离恢复</summary>
    <div className="space-y-2 mt-2">
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={busy} className="btn-ios-secondary" onClick={() => verify(false)}>校验SQL.gz</button>
        <button type="button" disabled={busy} className="btn-ios-secondary" onClick={() => verify(true)}>隔离库恢复验证</button>
      </div>
      {message && <p role="status">{message}</p>}
      {rows.map(record => <div key={record.id} className="border-t pt-2 space-y-1">
        <strong>{STATUS_LABELS[record.status] || record.status}</strong>
        {record.protected && <p>已设为保护点</p>}
        {(record.protected || ['checked', 'restored'].includes(record.status)) && <button
          type="button" disabled={busy} className="btn-ios-secondary" onClick={() => protect(record)}
        >{record.protected ? '取消保护' : '设为保护点'}</button>}
        <p className="break-all">SHA256: {record.sha256}</p>
        <pre className="whitespace-pre-wrap max-w-xs">{JSON.stringify(record.report, null, 2)}</pre>
      </div>)}
    </div>
  </details>
}
