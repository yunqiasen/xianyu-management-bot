import {useState} from 'react'
import {get} from '@/utils/request'
export function ItemOperationHistory({accountId}:{accountId:string}) {
  const [rows,setRows]=useState<any[]>([]),[error,setError]=useState('')
  const names:Record<string,string>={local_delete:'本地删除（未动平台）',platform_delete:'平台删除',platform_offline:'平台下架'}
  return <details className="vben-card p-3" onToggle={async e=>{
    if(!e.currentTarget.open)return
    try {setRows((await get<{data:any[]}>(`/api/v1/items/operations/${encodeURIComponent(accountId)}/history`)).data)}catch{setError('商品操作记录读取失败')}
  }}><summary>本地删除 / 平台下架 / 平台删除记录</summary>{error&&<p>{error}</p>}
    {rows.map((r,i)=><details key={i}><summary>{r.created_at} · {names[r.action]||r.action}</summary><pre className="text-xs whitespace-pre-wrap">{JSON.stringify(r.detail,null,2)}</pre></details>)}
  </details>
}
