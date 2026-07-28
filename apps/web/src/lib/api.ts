import type { ActivityItem, AgentStatus, AiConfig, Author, AuthorDigest, CatalogEvent, Chapter, ConfigAuthStatus, ConnectionTestResult, DashboardData, Job, MergeSuggestion, NotificationConfig, ReleaseSignal, SeriesCandidate, SocialAccount, SocialAccountSuggestion, SocialPost, SocialStatus, Source, SystemConfig, Work, WorkGroup, WorkGroupDetail } from '../types'

function cookie(name: string): string | undefined {
  return document.cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith(`${name}=`))?.slice(name.length + 1)
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options?.method && options.method !== 'GET' && cookie('mangafinder_csrf')
        ? { 'X-CSRF-Token': decodeURIComponent(cookie('mangafinder_csrf') ?? '') }
        : {}),
      ...options?.headers,
    },
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
  excludeWorkGroup: (groupId: number, authorId: number) =>
    request<void>(`/work-groups/${groupId}/authors/${authorId}`, { method: 'DELETE' }),
  excludeEdition: (groupId: number, workId: number, authorId: number) =>
    request<void>(`/work-groups/${groupId}/members/${workId}/authors/${authorId}`, { method: 'DELETE' }),
  seriesCandidates: (authorId?: number) =>
    request<SeriesCandidate[]>(`/series/candidates${authorId ? `?author_id=${authorId}` : ''}`),
  createSeriesFromCandidate: (candidate: SeriesCandidate) => request('/series', {
    method: 'POST',
    body: JSON.stringify({
      title: candidate.title,
      candidate: true,
      members: candidate.groups.map((item) => ({
        group_id: item.group_id, volume_number: item.volume_number,
      })),
    }),
  }),
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
  dashboard: (days = 30) => request<DashboardData>(`/dashboard?days=${days}`),
  discoveryInbox: (state = 'unread') =>
    request<CatalogEvent[]>(`/discovery-inbox?state=${encodeURIComponent(state)}`),
  updateDiscoveryEvent: (id: number, state: CatalogEvent['state']) =>
    request<void>(`/discovery-inbox/${id}`, {
      method: 'PATCH', body: JSON.stringify({ state }),
    }),
  updateAllDiscoveryEvents: (state: 'read' | 'ignored' = 'read') =>
    request<{ changed: number }>('/discovery-inbox/bulk', {
      method: 'POST', body: JSON.stringify({ state }),
    }),
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
  verifySocialPost: (postId: number) =>
    request<SocialPost>(`/social/posts/${postId}/verify`, { method: 'POST' }),
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
  configAuth: () => request<ConfigAuthStatus>('/system-config/auth'),
  bootstrapConfigAdmin: (password: string) =>
    request<ConfigAuthStatus>('/system-config/auth/bootstrap', { method: 'POST', body: JSON.stringify({ password }) }),
  loginConfigAdmin: (password: string) =>
    request<ConfigAuthStatus>('/system-config/auth/login', { method: 'POST', body: JSON.stringify({ password }) }),
  logoutConfigAdmin: () => request<void>('/system-config/auth/logout', { method: 'POST' }),
  systemConfig: () => request<SystemConfig>('/system-config'),
  saveSystemConfig: (config: Pick<SystemConfig, 'ai' | 'discovery' | 'radar' | 'notifications'>) =>
    request<{ config: SystemConfig, changed_keys: string[], restart_required_fields: string[] }>('/system-config', {
      method: 'PUT', body: JSON.stringify(config),
    }),
  testAgentConfig: (ai: AiConfig) => request<ConnectionTestResult>('/system-config/test/agent', {
    method: 'POST', body: JSON.stringify({ provider: ai.provider, base_url: ai.base_url, model: ai.model, api_key: ai.api_key }),
  }),
  testXConfig: () => request<ConnectionTestResult>('/system-config/test/x', { method: 'POST' }),
  testQqConfig: (notifications: NotificationConfig) => request<ConnectionTestResult>('/system-config/test/qq', {
    method: 'POST', body: JSON.stringify({ app_id: notifications.qq_app_id, client_secret: notifications.qq_client_secret, user_openid: notifications.qq_user_openid }),
  }),
  importXCookie: (cookieHeader: string) =>
    request<ConnectionTestResult>('/system-config/x-session', { method: 'PUT', body: JSON.stringify({ cookie_header: cookieHeader }) }),
  importXStorageState: (storageState: Record<string, unknown>) =>
    request<ConnectionTestResult>('/system-config/x-session', { method: 'PUT', body: JSON.stringify({ storage_state: storageState }) }),
  clearXSession: () => request<ConnectionTestResult>('/system-config/x-session', { method: 'DELETE' }),
}
