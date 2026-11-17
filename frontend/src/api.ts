export interface ApiErrorBody {
  code: string
  message: string
  streams?: number[]
  stream?: StreamView
  campaign?: CampaignView | null
  job?: Job
}

export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) {
    super(body.message)
  }
}

export interface Offer { id: number; name: string; state: string }
export interface Ref { id: number; name: string }
export interface References { domains: Ref[]; groups: Ref[]; traffic_sources: Ref[] }
export interface AppConfig {
  campaign_name_prefix: string
  redirect_url: string
  defaults: { domain_id: number | null; group_id: number | null; traffic_source_id: number | null }
  keitaro_configured: boolean
}

export type ViewState = 'published' | 'changed' | 'new' | 'restoring' | 'pending_remove' | 'archived'

export interface LinkView {
  offer_id: number
  offer_name: string
  offer_state: string
  status: 'active' | 'removed'
  share: number
  pinned: boolean
  kt_state: string
  pub_present: boolean
  pub_share: number | null
  view_state: ViewState
  dirty: boolean
}

export interface StreamView {
  id: number
  name: string
  position: number
  has_offers: boolean
  dirty: boolean
  draft_version: number
  push_state: 'idle' | 'pushing'
  last_error: string | null
  side_effect_fields: string[]
  reconciliation_required: boolean
  remote_missing: boolean
  synced_at: string | null
  active_sum: number
  links: LinkView[]
}

export interface CampaignView {
  id: number
  name: string
  alias: string
  synced_at: string | null
  push_allowed: boolean
  streams: StreamView[]
}

export interface PushResult {
  stream: StreamView
  result: 'published' | 'already_applied' | 'nothing_to_push' | 'side_effect_detected'
  warning?: string | null
  changed_fields?: string[]
}

export interface Job {
  idempotency_key: string
  status: 'pending' | 'running' | 'partial' | 'done' | 'failed'
  stale: boolean
  name: string
  countries: string[]
  offer_id: number
  alias: string
  campaign_id: number | null
  geo_stream_id: number | null
  offer_stream_id: number | null
  last_error: string | null
}

async function request<T>(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(url, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError(0, { code: 'network', message: 'Backend недоступен' })
  }
  const text = await resp.text()
  let data: unknown = null
  try { data = text ? JSON.parse(text) : null } catch { data = null }
  if (!resp.ok) {
    const b = data as Partial<ApiErrorBody> & { detail?: unknown }
    const message = b?.message ?? (Array.isArray(b?.detail) ? 'Некорректные данные запроса' : `Ошибка ${resp.status}`)
    throw new ApiError(resp.status, { code: b?.code ?? 'http_error', message, streams: b?.streams, stream: b?.stream,
      campaign: (b as Partial<ApiErrorBody>)?.campaign, job: (b as Partial<ApiErrorBody>)?.job })
  }
  return data as T
}

export const api = {
  config: () => request<AppConfig>('GET', '/api/config'),
  references: () => request<References>('GET', '/api/references'),
  searchOffers: (q: string, signal?: AbortSignal) =>
    request<Offer[]>('GET', `/api/offers?q=${encodeURIComponent(q)}&limit=15`, undefined, signal),
  createCampaign: (body: Record<string, unknown>) => request<Job>('POST', '/api/campaigns', body),
  resumeJob: (key: string) => request<Job>('POST', `/api/campaign-jobs/${encodeURIComponent(key)}/resume`),
  localCampaigns: () => request<{ id: number; name: string; synced_at: string | null }[]>('GET', '/api/campaigns'),
  campaign: (id: number) => request<CampaignView>('GET', `/api/campaigns/${id}`),
  fetchStreams: (id: number, discard = false) =>
    request<{ campaign: CampaignView; report: Record<string, unknown> }>('POST', `/api/campaigns/${id}/fetch`, { discard_drafts: discard }),
  addOffer: (s: StreamView, offerId: number) =>
    request<StreamView>('POST', `/api/streams/${s.id}/offers`, { expected_version: s.draft_version, offer_id: offerId }),
  linkOp: (s: StreamView, offerId: number, op: 'remove' | 'restore' | 'pin' | 'unpin', share?: number) =>
    request<StreamView>('POST', `/api/streams/${s.id}/offers/${offerId}/${op}`, { expected_version: s.draft_version, share }),
  cancel: (s: StreamView) => request<StreamView>('POST', `/api/streams/${s.id}/cancel`, { expected_version: s.draft_version }),
  push: (s: StreamView) =>
    request<PushResult>('POST', `/api/streams/${s.id}/push`, { expected_version: s.draft_version }),
}

/** В Keitaro названия офферов часто уже начинаются с ID — не дублируем его в интерфейсе. */
export function offerLabel(id: number, name: string): string {
  const n = (name ?? '').trim()
  return n.startsWith(`${id} `) || n === String(id) ? n.slice(String(id).length).trim() : n
}
