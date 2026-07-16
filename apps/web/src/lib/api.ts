import type { ActivityItem, AgentStatus, Author, AuthorDigest, Chapter, Job, MergeSuggestion, ReleaseSignal, SocialAccount, SocialAccountSuggestion, SocialPost, SocialStatus, Source, Work, WorkGroup, WorkGroupDetail } from '../types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail ?? `请求失败（${response.status}）`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  authors: () => request<Author[]>('/authors'),
  createAuthor: (name: string) =>
    request<Author>('/authors', { method: 'POST', body: JSON.stringify({ name }) }),
  deleteAuthor: (id: number) => request<void>(`/authors/${id}`, { method: 'DELETE' }),
  refreshAuthor: (id: number) => request<Job>(`/authors/${id}/refresh`, { method: 'POST' }),
  works: (authorId?: number) =>
    request<Work[]>(`/works${authorId ? `?author_id=${authorId}` : ''}`),
  workGroups: (authorId?: number) =>
    request<WorkGroup[]>(`/work-groups${authorId ? `?author_id=${authorId}` : ''}`),
  workGroup: (id: number) => request<WorkGroupDetail>(`/work-groups/${id}`),
  mergeGroups: (targetGroupId: number, sourceGroupId: number) =>
    request<WorkGroupDetail>(`/work-groups/${targetGroupId}/merge`, {
      method: 'POST', body: JSON.stringify({ source_group_id: sourceGroupId }),
    }),
  splitEdition: (groupId: number, workId: number) =>
    request<WorkGroupDetail>(`/work-groups/${groupId}/members/${workId}/split`, { method: 'POST' }),
  mergeSuggestions: () => request<MergeSuggestion[]>('/work-groups/suggestions'),
  acceptSuggestion: (id: number) =>
    request<WorkGroupDetail>(`/work-groups/suggestions/${id}/accept`, { method: 'POST' }),
  rejectSuggestion: (id: number) =>
    request<MergeSuggestion>(`/work-groups/suggestions/${id}/reject`, { method: 'POST' }),
  agentStatus: () => request<AgentStatus>('/agent-reviews/status'),
  runAgentReviews: (maxReviews?: number) =>
    request<Job>('/agent-reviews/run', {
      method: 'POST',
      body: JSON.stringify({ max_reviews: maxReviews ?? null }),
    }),
  jobs: () => request<Job[]>('/jobs?limit=10'),
  sources: () => request<Source[]>('/sources'),
  chapters: (workId: number, provider: string) =>
    request<Chapter[]>(`/works/${workId}/chapters?provider=${encodeURIComponent(provider)}`),
  downloadChapter: (workId: number, provider: string, chapterId: string) =>
    request<Job>(`/works/${workId}/downloads`, {
      method: 'POST', body: JSON.stringify({ provider, chapter_id: chapterId }),
    }),
  socialStatus: () => request<SocialStatus>('/social/status'),
  socialRadar: (authorId?: number, status?: string) => {
    const params = new URLSearchParams()
    if (authorId) params.set('author_id', String(authorId))
    if (status) params.set('status', status)
    return request<ReleaseSignal[]>(`/social/radar${params.size ? `?${params}` : ''}`)
  },
  socialAccounts: (authorId: number) =>
    request<SocialAccount[]>(`/authors/${authorId}/social-accounts`),
  socialAccountSuggestions: (authorId: number) =>
    request<SocialAccountSuggestion[]>(`/authors/${authorId}/social-account-suggestions`),
  socialActivity: (authorId?: number, category?: string) => {
    const params = new URLSearchParams()
    if (authorId) params.set('author_id', String(authorId))
    if (category) params.set('category', category)
    return request<ActivityItem[]>(`/social/activity${params.size ? `?${params}` : ''}`)
  },
  socialDigest: (authorId: number) =>
    request<AuthorDigest | null>(`/authors/${authorId}/social-digest`),
  refreshSocialDigest: (authorId: number) =>
    request<AuthorDigest | null>(`/authors/${authorId}/social-digest/refresh`, { method: 'POST' }),
  sendDailyDigest: () => request<Job>('/social/daily-digest/send', { method: 'POST' }),
  socialPosts: (authorId: number, postType?: string) => {
    const params = new URLSearchParams()
    if (postType) params.set('post_type', postType)
    return request<SocialPost[]>(`/authors/${authorId}/social-posts${params.size ? `?${params}` : ''}`)
  },
  markActivityRead: (activityId: number) =>
    request<void>(`/social/activity/${activityId}/read`, { method: 'POST' }),
  addSocialAccount: (authorId: number, handle: string, accountType: 'personal' | 'circle' = 'personal') =>
    request<SocialAccount>(`/authors/${authorId}/social-accounts`, {
      method: 'POST', body: JSON.stringify({ handle, account_type: accountType, confirmed: true }),
    }),
  confirmSocialAccount: (authorId: number, accountId: number) =>
    request<SocialAccount>(`/authors/${authorId}/social-accounts/${accountId}/confirm`, { method: 'POST' }),
  deleteSocialAccount: (authorId: number, accountId: number) =>
    request<void>(`/authors/${authorId}/social-accounts/${accountId}`, { method: 'DELETE' }),
  syncSocial: (authorId: number) => request<Job[]>(`/authors/${authorId}/social-sync`, { method: 'POST' }),
  reviewSocialSignal: (signalId: number, decision: 'confirm' | 'reject') =>
    request<ReleaseSignal>(`/social/signals/${signalId}/review`, {
      method: 'POST', body: JSON.stringify({ decision }),
    }),
  linkSocialSignal: (signalId: number, groupId: number) =>
    request<ReleaseSignal>(`/social/signals/${signalId}/link-work`, {
      method: 'POST', body: JSON.stringify({ group_id: groupId }),
    }),
  markSocialSignalRead: (signalId: number) =>
    request<void>(`/social/signals/${signalId}/read`, { method: 'POST' }),
}
