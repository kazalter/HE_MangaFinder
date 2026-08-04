import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Author, ReleaseSignal, SocialStatus, WorkGroup } from '../types'
import { SocialRadar } from './SocialRadar'

const apiMocks = vi.hoisted(() => ({
  socialAccounts: vi.fn(),
  socialActivity: vi.fn(),
  socialDigest: vi.fn(),
  socialPosts: vi.fn(),
  socialRadar: vi.fn(),
}))

vi.mock('../lib/api', () => ({ api: apiMocks }))

const authors: Author[] = [
  { id: 1, name: '作者甲', avatar_url: null, x_handle: 'author_a', x_display_name: null, x_last_synced_at: '2026-07-19T06:00:00Z', x_sync_error: null, created_at: '', last_checked_at: null, work_count: 12 },
  { id: 2, name: '作者乙', avatar_url: null, x_handle: 'author_b', x_display_name: null, x_last_synced_at: null, x_sync_error: null, created_at: '', last_checked_at: null, work_count: 8 },
]

const status: SocialStatus = {
  enabled: true,
  collector_configured: true,
  agent_configured: true,
  qq_configured: false,
  webhook_configured: false,
  auto_confirm_threshold: 0.92,
  candidate_threshold: 0.6,
  pending_count: 0,
  unread_count: 0,
  daily_digest_enabled: false,
  daily_digest_hour: 20,
  daily_digest_timezone: 'Asia/Shanghai',
}

describe('SocialRadar author rail', () => {
  beforeEach(() => {
    window.localStorage.clear()
    apiMocks.socialAccounts.mockResolvedValue([])
    apiMocks.socialActivity.mockResolvedValue([])
    apiMocks.socialDigest.mockResolvedValue(null)
    apiMocks.socialPosts.mockResolvedValue([])
    apiMocks.socialRadar.mockResolvedValue([])
  })

  afterEach(cleanup)

  it('switches authors inside the radar and remembers the radar-only choice', async () => {
    render(
      <SocialRadar
        status={status}
        signals={[]}
        authors={authors}
        works={[]}
        selectedAuthorId={null}
        busy={false}
        focusSignalId={null}
        onClose={vi.fn()}
        onChanged={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={vi.fn()}
      />,
    )

    await waitFor(() => expect(apiMocks.socialActivity).toHaveBeenCalledWith(1))
    expect(screen.getByRole('heading', { name: '作者甲' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /作者乙/ }))
    await waitFor(() => expect(apiMocks.socialActivity).toHaveBeenCalledWith(2))
    expect(screen.getByRole('heading', { name: '作者乙' })).toBeInTheDocument()
    expect(window.localStorage.getItem('mangafinder:radar-author')).toBe('2')

    fireEvent.click(screen.getByRole('button', { name: '账号与同步' }))
    expect(screen.getByText('作者乙 的 X 账号')).toBeInTheDocument()
  })

  it('creates the large work selector only when a signal is being linked', async () => {
    const signal: ReleaseSignal = {
      id: 10, author_id: 1, author_name: '作者甲', kind: 'new_release', title: '新作',
      event_code: null, booth: null, release_date: null, store_urls: [], confidence: .8,
      status: 'pending', is_read: false, evidence: [], counter_evidence: [], missing_information: [],
      planned_release_id: null, linked_group_id: null, reviewed_by: null, created_at: '', updated_at: '', posts: [],
    }
    const works = Array.from({ length: 100 }, (_, index): WorkGroup => ({
      id: index + 1, title: `作品 ${index + 1}`, description: null, cover_url: null, status: null,
      year: null, language: null, tags: [], first_source_at: null, latest_source_at: null,
      edition_count: 1, providers: ['wnacg'], authors: [{ id: 1, name: '作者甲' }],
    }))
    apiMocks.socialRadar.mockResolvedValue([signal])
    render(
      <SocialRadar status={status} signals={[signal]} authors={authors} works={works} selectedAuthorId={1} busy={false} focusSignalId={null}
        onClose={vi.fn()} onChanged={vi.fn().mockResolvedValue(undefined)} onOpenWork={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: '作品情报' }))
    await screen.findByRole('heading', { name: '新作' })
    expect(screen.queryByRole('option', { name: '作品 100' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '关联现有作品' }))
    expect(screen.getByRole('option', { name: '作品 100' })).toBeInTheDocument()
  })

  it('filters the author rail by name or X handle', async () => {
    render(
      <SocialRadar
        status={status}
        signals={[]}
        authors={authors}
        works={[]}
        selectedAuthorId={1}
        busy={false}
        focusSignalId={null}
        onClose={vi.fn()}
        onChanged={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByLabelText('搜索雷达作者'), { target: { value: 'author_b' } })
    expect(screen.getByRole('button', { name: /作者乙/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /作者甲/ })).not.toBeInTheDocument()
  })
})
