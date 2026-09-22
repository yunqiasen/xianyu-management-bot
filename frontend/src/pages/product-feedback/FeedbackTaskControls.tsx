import { runProductFeedback, getFeedbackHistory, type FeedbackResult } from '@/api/productFeedback'
export function FeedbackTaskControls({accountId,kind,orderNos,startDate,endDate,onDone,onError}: {
  accountId:string;kind:'rate'|'red_flower';orderNos:string;startDate:string;endDate:string;
  onDone:(rows:FeedbackResult[])=>void;onError:(message:string)=>void;
}) {
  async function run(history:boolean) {
    try {
      const r=await runProductFeedback({account_id:accountId,kind,
        ...(history?{start_date:startDate,end_date:endDate}:{order_nos:orderNos.split(/[\s,，]+/).filter(Boolean)})})
      if(r.success) onDone(r.data); else onError('执行未确认，请查历史记录')
    } catch { onError('执行中断，请先查历史记录，不要重复提交待核实项') }
  }
  return <div className="flex flex-wrap gap-2 my-3">
    <button className="btn-ios-primary" disabled={!accountId || !orderNos.trim()} onClick={()=>run(false)}>立即执行指定订单</button>
    <button className="btn-ios-secondary" disabled={!accountId || !startDate || !endDate} onClick={()=>run(true)}>历史补处理</button>
    <button className="btn-ios-secondary" disabled={!accountId} onClick={async()=>{
      try {onDone((await getFeedbackHistory(accountId)).data)} catch {onError('历史读取失败')}
    }}>查看逐项历史</button>
  </div>
}
