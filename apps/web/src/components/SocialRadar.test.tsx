import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Author, SocialStatus } from '../types'
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
