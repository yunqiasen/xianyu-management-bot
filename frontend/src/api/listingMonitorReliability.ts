import { get, put } from '@/utils/request'
import type { ApiResponse } from '@/types'

export interface MonitorReliabilityStatus {
  enabled: boolean
  release_gate: string
  baseline_ready: boolean
  phase: string
  generation: number
  expected_pages?: number
  region: string
  pages: { page: number; status: string; account_id?: string }[]
  events: {
    id: string; item_id: string; kind: string; summary: string
    price: string; old_price?: string | null; enqueue_status: string
    notification_event_id?: string | null
    deliveries?: { channel_id: number; status: string; attempts: number }[]
  }[]
  items: { item_id: string; title: string; price: string; area: string }[]
}

const prefix = '/api/v1/product-monitor/listing-tasks'
export const getMonitorReliability = (taskId: number): Promise<ApiResponse<MonitorReliabilityStatus>> =>
  get(`${prefix}/${taskId}/reliability`)
export const saveMonitorRegion = (taskId: number, region: string): Promise<ApiResponse> =>
  put(`${prefix}/${taskId}/reliability`, { region })
