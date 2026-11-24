/** 坐标格时窗口接口：登记、巡检领取、审核、正式版本与逐点复算。 */
import { request } from './client'

export type WindowCell = {
  window_id: string
  geohash: string | null
  time_bucket: number
  time_bucket_str: string | null
  observation_status: string
  sample_eligibility: string
  empty_reason: string
  observation_count: number
  valid_count: number
  provisional_estimate: number | null
  formal_estimate: number | null
  formal_version: number | null
}

export type WindowList = {
  ok: boolean
  total: number
  windows: WindowCell[]
  empty_reason: string
  eligibility_counts: Record<string, number>
  status_counts: Record<string, number>
}

export type ClaimSummary = {
  claim_id: string
  batch_key: string
  claim_kind: string
  status: string
  scope: string
  delivery: string | null
  formal_version: number | null
  window_count: number
  provisional_total: number | null
  created_at: number
  confirmed_at: number | null
}

export type ClaimList = {
  ok: boolean
  total: number
  claims: ClaimSummary[]
  empty_reason: string
}

export type ReplayWindow = {
  window_id: string
  observation_status: string
  sample_eligibility: string
  claimed_estimate: number | null
  recomputed_estimate: number
  matches: boolean
  points: {
    point_id: string | number
    adopted_lat: number | null
    adopted_lon: number | null
    coordinate_source: string
    valid: boolean
    contribution: number
  }[]
}

export type ReplayResult = {
  ok: boolean
  claim_id: string
  batch_key: string
  claim_kind: string
  status: string
  delivery: string | null
  formal_version: number | null
  estimate_kind: string
  claimed_total: number | null
  recomputed_total: number
  all_match: boolean
  windows: ReplayWindow[]
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const payload = (await response.json()) as { detail?: string }
      detail = payload.detail ?? fallback
    } catch {
      // 保留默认文案
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export async function listWindows(batchKey: string): Promise<WindowList> {
  const query = new URLSearchParams({ batch_key: batchKey })
  const response = await request(`/api/coord-windows?${query}`)
  return readJson<WindowList>(response, '格时窗口读取失败')
}

export async function buildWindows(batchKey: string): Promise<unknown> {
  const response = await request('/api/coord-windows/build', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey }),
  })
  return readJson<unknown>(response, '格时窗口登记失败')
}

export async function listClaims(batchKey: string): Promise<ClaimList> {
  const query = new URLSearchParams({ batch_key: batchKey })
  const response = await request(`/api/coord-windows/claims?${query}`)
  return readJson<ClaimList>(response, '领取记录读取失败')
}

export async function claimInspection(batchKey: string): Promise<{ claim_id: string }> {
  const response = await request('/api/coord-windows/claims/inspection', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey }),
  })
  return readJson<{ claim_id: string }>(response, '巡检领取失败')
}

export async function claimFormal(
  batchKey: string,
  scope: 'all' | 'zero_only',
): Promise<{ claim_id: string; delivery: string; window_ids: string[] }> {
  const response = await request('/api/coord-windows/claims/formal', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey, scope }),
  })
  return readJson<{ claim_id: string; delivery: string; window_ids: string[] }>(
    response,
    '正式领取失败',
  )
}

export async function confirmClaim(claimId: string): Promise<{ delivery: string; formal_version: number }> {
  const response = await request(
    `/api/coord-windows/claims/${encodeURIComponent(claimId)}/confirm`,
    { method: 'POST', body: JSON.stringify({}) },
  )
  return readJson<{ delivery: string; formal_version: number }>(response, '领取确认失败')
}

export async function replayClaim(claimId: string): Promise<ReplayResult> {
  const response = await request(
    `/api/coord-windows/claims/${encodeURIComponent(claimId)}/replay`,
  )
  return readJson<ReplayResult>(response, '领取复算失败')
}
