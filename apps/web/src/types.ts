export interface Author {
  id: number
  name: string
  created_at: string
  last_checked_at: string | null
  work_count: number
}

export interface WorkSource {
  provider: string
  external_id: string
  source_url: string
  source_updated_at: string | null
}

export interface Work {
  id: number
  title: string
  description: string | null
  cover_url: string | null
  status: string | null
  year: number | null
  language: string | null
  tags: string[]
  discovered_at: string
  sources: WorkSource[]
}

export interface WorkGroup {
  id: number
  title: string
  description: string | null
  cover_url: string | null
  status: string | null
  year: number | null
  language: string | null
  tags: string[]
  latest_source_at: string | null
  edition_count: number
  providers: string[]
}

export interface Edition {
  work_id: number
  title: string
  description: string | null
  cover_url: string | null
  status: string | null
  year: number | null
  language: string | null
  tags: string[]
  variant_labels: string[]
  latest_source_at: string | null
  confidence: number
  match_method: string
  is_manual: boolean
  sources: WorkSource[]
}

export interface WorkGroupDetail extends WorkGroup {
  editions: Edition[]
}

export interface MergeSuggestion {
  id: number
  source_group_id: number
  source_title: string
  target_group_id: number
  target_title: string
  source_group: WorkGroup | null
  target_group: WorkGroup | null
  confidence: number
  reasons: string[]
  status: 'pending' | 'accepted' | 'rejected'
  agent_review: AgentReview | null
  hard_conflicts: string[]
  soft_conflicts: string[]
  conflict_details: string[]
  core_title_similarity: number | null
  cover_hash_distance: number | null
  source_identity_titles: string[]
  target_identity_titles: string[]
  shared_context: string[]
}

export interface AgentReview {
  id: number
  suggestion_id: number | null
  status: 'succeeded' | 'failed' | 'blocked' | 'invalidated'
  decision: 'same_work' | 'different_work' | 'uncertain' | null
  confidence: number | null
  relation: string | null
  canonical_title: string | null
  evidence_codes: string[]
  conflict_codes: string[]
  rationale: string | null
  model: string
  prompt_version: string
  error: string | null
  created_at: string
  is_stale: boolean
}

export interface AgentStatus {
  enabled: boolean
  configured: boolean
  provider: string
  model: string
  prompt_version: string
  auto_apply: boolean
  sends_images: boolean
  review_after_discovery: boolean
}

export interface Job {
  id: number
  kind: string
  payload: Record<string, unknown>
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  attempts: number
  error: string | null
}

export interface Chapter {
  external_id: string
  title: string | null
  number: string | null
  language: string
  published_at: string | null
  source_url: string
}

export interface Source {
  name: string
  display_name: string
  capabilities: string[]
}

export interface SocialAccount {
  id: number
  author_id: number
  platform: string
  handle: string
  display_name: string | null
  profile_url: string | null
  avatar_url: string | null
  account_type: 'personal' | 'circle'
  status: 'suggested' | 'confirmed' | 'paused' | 'error'
  match_score: number | null
  evidence: string[]
  last_synced_at: string | null
  next_sync_at: string | null
  sync_error: string | null
}

export interface SocialAccountSuggestion {
  handle: string
  display_name: string | null
  profile_url: string
  avatar_url: string | null
  score: number
  evidence: string[]
}

export interface SocialPost {
  id: number
  platform_post_id: string
  post_type: 'original' | 'reply' | 'quote' | 'retweet'
  text: string
  url: string
  media: Array<{ type?: string; url?: string; alt_text?: string }>
  links: string[]
  ocr_text: string | null
  posted_at: string
}

export interface ReleaseSignal {
  id: number
  author_id: number
  author_name: string
  kind: string
  title: string | null
  event_code: string | null
  booth: string | null
  release_date: string | null
  store_urls: string[]
  confidence: number
  status: 'pending' | 'confirmed' | 'rejected' | 'linked' | 'released' | 'cancelled' | 'archived'
  is_read: boolean
  evidence: string[]
  counter_evidence: string[]
  missing_information: string[]
  linked_group_id: number | null
  reviewed_by: string | null
  created_at: string
  updated_at: string
  posts: SocialPost[]
}

export interface SocialStatus {
  enabled: boolean
  collector_configured: boolean
  agent_configured: boolean
  qq_configured: boolean
  auto_confirm_threshold: number
  candidate_threshold: number
  pending_count: number
  unread_count: number
}
