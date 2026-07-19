export interface Author {
  id: number
  name: string
  avatar_url: string | null
  x_handle: string | null
  x_display_name: string | null
  x_last_synced_at: string | null
  x_sync_error: string | null
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
  first_source_at: string | null
  latest_source_at: string | null
  edition_count: number
  providers: string[]
  authors: Pick<Author, 'id' | 'name'>[]
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
  cover_match_mode: 'full' | 'crop' | 'legacy' | null
  cover_legacy_distance: number | null
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
  created_at: string
  started_at: string | null
  finished_at: string | null
  next_attempt_at: string | null
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
  daily_digest_enabled: boolean
  daily_digest_hour: number
  daily_digest_timezone: string
}

export interface ConfigAuthStatus {
  initialized: boolean
  authenticated: boolean
  username: string | null
}

export interface AiConfig {
  enabled: boolean
  provider: 'openai_compatible' | 'deepseek'
  base_url: string
  model: string
  api_key: string | null
  api_key_configured: boolean
  temperature: number
  timeout_seconds: number
  review_after_discovery: boolean
}

export interface RadarConfig {
  enabled: boolean
  sync_interval_minutes: number
  event_sync_interval_minutes: number
  initial_backfill_days: number
  max_posts_per_sync: number
  agent_enabled: boolean
  candidate_threshold: number
  auto_confirm_threshold: number
  ocr_enabled: boolean
  ocr_max_posts_per_sync: number
  ocr_timeout_seconds: number
}

export interface NotificationConfig {
  daily_digest_enabled: boolean
  daily_digest_hour: number
  daily_digest_timezone: string
  daily_digest_initial_lookback_days: number
  daily_digest_min_importance: 'low' | 'normal' | 'high'
  daily_digest_max_authors: number
  daily_digest_max_items_per_author: number
  qq_enabled: boolean
  qq_app_id: string
  qq_client_secret: string | null
  qq_client_secret_configured: boolean
  qq_user_openid: string
}

export interface SystemConfig {
  ai: AiConfig
  radar: RadarConfig
  notifications: NotificationConfig
  x_session: {
    configured: boolean
    collector_reachable: boolean
    valid: boolean | null
    provider: string | null
    last_error: string | null
    proxy_configured: boolean
    user_agent_configured: boolean
  }
  deployment: {
    api_running: boolean
    collector_running: boolean
    social_profile_required: boolean
    x_session_dir: string
    social_media_dir: string
    database_url: string
    restart_required_fields: string[]
  }
}

export interface ConnectionTestResult {
  ok: boolean
  message: string
}

export interface ActivityItem {
  id: number
  author_id: number
  author_name: string
  category: string
  headline: string
  summary: string
  importance: 'critical' | 'high' | 'normal' | 'low'
  confidence: number
  is_read: boolean
  started_at: string
  ended_at: string
  posts: SocialPost[]
}

export interface DigestHighlight {
  text: string
  category: string
  importance: 'critical' | 'high' | 'normal' | 'low'
  factuality: 'fact' | 'plan' | 'inference'
  post_ids: number[]
}

export interface AuthorDigest {
  id: number
  author_id: number
  author_name: string
  period_type: string
  period_start: string
  period_end: string
  summary: string
  highlights: DigestHighlight[]
  uncertainties: string[]
  evidence_post_ids: number[]
  generated_by: 'agent' | 'rules'
  model: string | null
  error: string | null
  created_at: string
  updated_at: string
}
