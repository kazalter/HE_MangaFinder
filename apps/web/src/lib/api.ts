import type { AgentStatus, Author, Chapter, Job, MergeSuggestion, Source, Work, WorkGroup, WorkGroupDetail } from '../types'

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
}
