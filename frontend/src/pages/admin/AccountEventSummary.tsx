import { useState,useEffect } from 'react'
import { get } from '@/utils/request'
interface Row {category:string;reason:string;status:string;count:number}
const labels:Record<string,string>={rate_limit:'限流',credentials:'凭据',manual_verification:'人工验证',other:'其他/未分类'}
export function AccountEventSummary(){
 const [rows,setRows]=useState<Row[]>([]),[days,setDays]=useState(7),[error,setError]=useState('')
 useEffect(()=>{let current=true;setError('');get<{data:Row[]}>(`/api/v1/account-login-logs/summary?days=${days}`).then(r=>{if(current)setRows(r.data)}).catch(()=>{if(current)setError('账号事件统计加载失败')});return()=>{current=false}},[days])
 return <section className="vben-card p-4 space-y-2"><h2 className="font-semibold">账号事件分类</h2><select aria-label="事件统计天数" className="input-ios max-w-xs" value={days} onChange={e=>setDays(Number(e.target.value))}><option value={1}>近1天</option><option value={7}>近7天</option><option value={30}>近30天</option></select><p className="text-xs text-slate-500">来源：账号登录日志；限流、凭据、人工验证分别计数。跳过与失败分开，未知原因保留未分类，不猜测。</p>{error&&<p role="alert">{error}</p>}<div className="overflow-auto"><table className="table-ios"><thead><tr><th>类别</th><th>源原因</th><th>处理状态</th><th>次数</th></tr></thead><tbody>{rows.map(r=><tr key={r.reason+r.status}><td>{labels[r.category]}</td><td>{r.reason}</td><td>{r.status}</td><td>{r.count}</td></tr>)}</tbody></table>{!rows.length&&!error&&<p>暂无事件记录</p>}</div></section>
}
