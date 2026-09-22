import { useState, useEffect } from 'react'
import { getAccountDetails, updateAccountScheduledRate, updateAccountAutoRedFlower } from '@/api/accounts'
import { getAutoRateConfig, updateAutoRateConfig, type AutoRateConfig } from '@/api/autoRate'
import { getFeedbackTaskConfig, getRateTemplates, createRateTemplate, editRateTemplate, activateRateTemplate, deleteRateTemplate, type RateTemplate, type FeedbackResult } from '@/api/productFeedback'
import { FeedbackTaskControls } from './FeedbackTaskControls'

const labels:Record<string,string>={success:'成功',failed:'明确失败',unknown:'结果待核实',skipped:'跳过',already_processed:'已处理',cooldown:'冷却中',duplicate_plan:'重复计划',disabled:'未启用',inapplicable:'不适用',order_not_found:'该账号未同步此订单',account_mismatch:'账号不符'}
export function ProductFeedbackPanel({kind}:{kind:'rate'|'red_flower'}) {
  const [accounts,setAccounts]=useState<any[]>([]), [accountId,setAccountId]=useState('')
  const [scheduledRate,setScheduledRate]=useState(false)
  const [config,setConfig]=useState<AutoRateConfig|null>(null),[enabled,setEnabled]=useState(false)
  const [templates,setTemplates]=useState<RateTemplate[]>([]),[name,setName]=useState(''),[content,setContent]=useState('')
  const [orderNos,setOrderNos]=useState(''),[startDate,setStartDate]=useState(''),[endDate,setEndDate]=useState('')
  const [rows,setRows]=useState<FeedbackResult[]>([]),[message,setMessage]=useState('')
  useEffect(()=>{getAccountDetails().then(setAccounts).catch(()=>setMessage('账号列表读取失败'))},[])
  useEffect(()=>{
    let active=true
    setRows([]);setTemplates([]);setConfig(null);setMessage('')
    if(accountId) getFeedbackTaskConfig(accountId).then(r=>{if(active)setScheduledRate(r.data.scheduled_rate)}).catch(()=>{if(active)setMessage('定时开关读取失败')})
    if(accountId && kind==='rate') {
      getAutoRateConfig(accountId).then(r=>{if(active){setConfig(r.data);setEnabled(r.data.enabled)}}).catch(()=>{if(active)setMessage('开关读取失败')})
      getRateTemplates(accountId).then(r=>{if(active)setTemplates(r.data)}).catch(()=>{if(active)setMessage('模板读取失败')})
    } else if(accountId) {
      getFeedbackTaskConfig(accountId).then(r=>{if(active)setEnabled(r.data.auto_red_flower)}).catch(()=>{if(active)setMessage('开关读取失败')})
    } else setEnabled(false)
    return ()=>{active=false}
  },[accountId,kind,accounts])
  async function refreshTemplates(){setTemplates((await getRateTemplates(accountId)).data)}
  async function toggle(){
    try {
      if(kind==='rate' && config){await updateAutoRateConfig(accountId,{...config,enabled:!enabled});setConfig({...config,enabled:!enabled})}
      else if(kind==='red_flower')await updateAccountAutoRedFlower(accountId,!enabled)
      else return
      setEnabled(!enabled);setMessage('开关已保存')
    } catch {setMessage('开关保存失败')}
  }
  return <section className="vben-card p-4 mb-4 space-y-3" aria-label="评价求花操作">
    <h2 className="font-semibold">{kind==='rate'?'评价模板与补处理':'小红花任务与补处理'}</h2>
    <p className="text-sm text-slate-500">小红花是向买家求评价，不是账号保活。已处理、重复和冷却项跳过；待核实结果停止自动重发。</p>
    <select className="input-ios" aria-label="执行账号" value={accountId} onChange={e=>setAccountId(e.target.value)}>
      <option value="">选择账号</option>{accounts.map(a=><option key={a.id} value={a.id}>{a.note||a.id}</option>)}
    </select>
    <button className="btn-ios-secondary" disabled={!accountId || (kind==='rate'&&!config)} onClick={toggle}>{enabled?(kind==='rate'?'关闭自动评价':'关闭自动求花'):(kind==='rate'?'开启自动评价':'开启自动求花')}</button>
    {kind==='rate'&&<button className="btn-ios-secondary" disabled={!accountId} onClick={async()=>{try{await updateAccountScheduledRate(accountId,!scheduledRate);setScheduledRate(!scheduledRate)}catch{setMessage('定时开关保存失败')}}}>{scheduledRate?'关闭定时补评价':'开启定时补评价'}</button>}
    {kind==='rate'&&accountId&&<details><summary>评价模板（激活不改变原开关）</summary>
      <p className="text-sm">当前来源：{config?.rate_type==='api'?'外部评价接口':config?.text_content||'尚未配置'}</p>
      <input aria-label="模板名称" className="input-ios" placeholder="模板名称" maxLength={80} value={name} onChange={e=>setName(e.target.value)}/>
      <textarea aria-label="评价模板内容" className="input-ios" maxLength={500} value={content} onChange={e=>setContent(e.target.value)} />
      <button className="btn-ios-primary" disabled={!name.trim()||!content.trim()} onClick={async()=>{
        try {await createRateTemplate(accountId,name,content);await refreshTemplates()}catch{setMessage('模板创建失败')}
      }}>新建模板</button>
      {templates.map(t=><div key={t.id} className="border-b py-2"><b>{t.name}{t.active?'（已激活）':''}</b><p>{t.content}</p>
        <button className="btn-ios-secondary" onClick={async()=>{try{await activateRateTemplate(accountId,t.id);await refreshTemplates();setConfig((await getAutoRateConfig(accountId)).data)}catch{setMessage('激活失败')}}}>激活</button>
        <button className="btn-ios-secondary" onClick={async()=>{const text=window.prompt('修改评价内容',t.content);if(!text)return;try{await editRateTemplate(accountId,t.id,t.name,text);await refreshTemplates()}catch{setMessage('修改失败')}}}>编辑</button>
        <button className="btn-ios-danger" disabled={t.active} onClick={async()=>{try{await deleteRateTemplate(accountId,t.id);await refreshTemplates()}catch{setMessage('删除失败')}}}>删除</button>
      </div>)}
    </details>}
    <input aria-label="订单号" className="input-ios" placeholder="订单号，逗号分隔（至多100笔）" value={orderNos} onChange={e=>setOrderNos(e.target.value)}/>
    <label>历史起始 <input aria-label="历史起始" type="date" value={startDate} onChange={e=>setStartDate(e.target.value)}/></label>
    <label> 截止 <input aria-label="历史截止" type="date" value={endDate} onChange={e=>setEndDate(e.target.value)}/></label>
    <FeedbackTaskControls accountId={accountId} kind={kind} orderNos={orderNos} startDate={startDate} endDate={endDate} onDone={setRows} onError={setMessage}/>
    {message&&<p role="status">{message}</p>}
    {rows.length>0&&<div className="overflow-auto"><table className="w-full text-sm"><thead><tr><th>订单</th><th>任务</th><th>结果</th><th>原因 / 时间</th></tr></thead><tbody>
      {rows.map((r,i)=><tr key={i}><td>{r.order_no}</td><td>{r.kind==='rate'?'评价':'求花'}</td><td>{labels[r.status]||r.status}</td><td>{labels[r.reason||'']||r.message||r.reason} {r.created_at}</td></tr>)}
    </tbody></table></div>}
  </section>
}
