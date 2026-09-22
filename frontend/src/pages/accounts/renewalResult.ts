export interface RenewalCounts {
  success_count?: number
  failed_count?: number
  skipped_count?: number
  unknown_count?: number
}

export function renewalToast(data: RenewalCounts | undefined): {type: 'success' | 'warning'; message: string} {
  const success = data?.success_count ?? 0
  const failed = data?.failed_count ?? 0
  const skipped = data?.skipped_count ?? 0
  const unknown = data?.unknown_count ?? 0
  return {
    type: success > 0 && failed + skipped + unknown === 0 ? 'success' : 'warning',
    message: `续期：成功 ${success}，失败 ${failed}，跳过 ${skipped}，待核实 ${unknown}`,
  }
}
