import { get, post, put, del } from '@/utils/request'
const prefix='/api/v1/auto-rate'
export type FeedbackResult={order_no:string; kind:string; status:string; reason?:string; message?:string; created_at?:string}
export type RateTemplate={id:number; name:string; content:string; active:boolean}
export const runProductFeedback=(payload:{account_id:string;kind:'rate'|'red_flower';order_nos?:string[];start_date?:string;end_date?:string}):Promise<{success:boolean;data:FeedbackResult[]}>=>post(`${prefix}/tasks/run`,payload,{timeout:180000})
export const getFeedbackHistory=(accountId:string):Promise<{success:boolean;data:FeedbackResult[]}>=>get(`${prefix}/tasks/history?account_id=${encodeURIComponent(accountId)}`)
export const getRateTemplates=(accountId:string):Promise<{success:boolean;data:RateTemplate[]}>=>get(`${prefix}/templates/${encodeURIComponent(accountId)}`)
export const createRateTemplate=(accountId:string,name:string,content:string)=>post(`${prefix}/templates/${encodeURIComponent(accountId)}`,{name,content})
export const editRateTemplate=(accountId:string,id:number,name:string,content:string)=>put(`${prefix}/templates/${encodeURIComponent(accountId)}/${id}`,{name,content})
export const activateRateTemplate=(accountId:string,id:number)=>post(`${prefix}/templates/${encodeURIComponent(accountId)}/${id}/activate`)
export const deleteRateTemplate=(accountId:string,id:number)=>del(`${prefix}/templates/${encodeURIComponent(accountId)}/${id}`)
export const getFeedbackTaskConfig=(accountId:string):Promise<{success:boolean;data:{auto_red_flower:boolean;scheduled_rate:boolean}} >=>get(`${prefix}/tasks/config/${encodeURIComponent(accountId)}`)
