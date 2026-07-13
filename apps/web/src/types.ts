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
  confidence: number
  reasons: string[]
  status: 'pending' | 'accepted' | 'rejected'
  agent_review: AgentReview | null
  hard_conflicts: string[]
  soft_conflicts: string[]
  conflict_details: string[]
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
}

export interface AgentStatus {
  enabled: boolean
  configured: boolean
  provider: string
  model: string
  prompt_version: string
  auto_apply: boolean
  sends_images: boolean
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
