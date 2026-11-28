import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Author, ReleaseSignal, SocialStatus, WorkGroup } from '../types'
import { AuthorXAccountsPanel, SocialRadar } from './SocialRadar'

const apiMocks = vi.hoisted(() => ({
  socialAccounts: vi.fn(),
  socialActivity: vi.fn(),
  socialDigest: vi.fn(),
  socialPosts: vi.fn(),
  socialRadar: vi.fn(),
}))

vi.mock('../lib/api', () => ({ api: apiMocks }))

const author: Author = {
  id: 1,
  name: '作者甲',
  avatar_url: null,
  x_handle: 'author_a',
  x_display_name: null,
  x_last_synced_at: '2026-07-19T06:00:00Z',
  x_sync_error: null,
  created_at: '',
  last_checked_at: null,
  work_count: 12,
}

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

describe('SocialRadar inline panel', () => {
  beforeEach(() => {
    window.localStorage.clear()
    apiMocks.socialAccounts.mockResolvedValue([])
    apiMocks.socialActivity.mockResolvedValue([])
    apiMocks.socialDigest.mockResolvedValue(null)
    apiMocks.socialPosts.mockResolvedValue([])
    apiMocks.socialRadar.mockResolvedValue([])
  })

  afterEach(cleanup)

  it('loads author feed views and binds X accounts cleanly in accounts panel', async () => {
    const { rerender } = render(
      <SocialRadar
        author={author}
        status={status}
        works={[]}
        busy={false}
        focusSignalId={null}
        onChanged={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={vi.fn()}
      />,
    )

    await waitFor(() => expect(apiMocks.socialActivity).toHaveBeenCalledWith(1))
    expect(screen.getByRole('button', { name: '立即扫描动态' })).toBeInTheDocument()

    rerender(
      <AuthorXAccountsPanel
        author={author}
        status={status}
        busy={false}
        onChanged={vi.fn().mockResolvedValue(undefined)}
      />,
    )
    await waitFor(() => expect(apiMocks.socialAccounts).toHaveBeenCalledWith(1))
    expect(screen.getByText('作者甲 的 X 账号')).toBeInTheDocument()
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
      <SocialRadar
        author={author}
        status={status}
        works={works}
        busy={false}
        focusSignalId={null}
        onChanged={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /作品情报/ }))
    await screen.findByRole('heading', { name: '新作' })
    expect(screen.queryByRole('option', { name: '作品 100' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '关联现有作品' }))
    expect(screen.getByRole('option', { name: '作品 100' })).toBeInTheDocument()
  })
})
