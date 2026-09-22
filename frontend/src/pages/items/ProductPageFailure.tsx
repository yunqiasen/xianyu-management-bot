export type ProductPageFailureData = {status?:string;failed_page?:number;retry_after?:number|null;retry_action?:string;partial?:boolean;saved_count?:number}
const actions:Record<string,string>={restore_session:'等待账号恢复',manual_verification:'需要人工验证',repair_proxy:'修复固定代理后继续',inspect_response:'检查平台响应结构',repair_storage:'检查存储状态',wait_budget:'等待账号预算'}
export function ProductPageFailure({data}:{data:ProductPageFailureData|null}) {
  if(!data?.status || ['complete','empty'].includes(data.status))return null
  return <p role="status" className="text-sm text-amber-600 my-2">
    {data.failed_page ? `第${data.failed_page}页失败。` : ''}
    {data.partial ? `已保留确认结果${data.saved_count!==undefined?`（入库${data.saved_count}项）`:''}。` : ''}
    {actions[data.retry_action||'']||data.status}
    {data.retry_after ? `，${data.retry_after}秒后可申请重试；以账号预算排队结果为准。` : '，不自动重试本页。'}
  </p>
}
