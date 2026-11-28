import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Author, SocialStatus, WorkGroup } from '../types'
import { AuthorDirectory } from './AuthorDirectory'

vi.mock('../lib/api', () => ({
  api: {
    socialAccounts: vi.fn().mockResolvedValue([]),
    socialAccountSuggestions: vi.fn().mockResolvedValue([]),
    socialActivity: vi.fn().mockResolvedValue([]),
    socialDigest: vi.fn().mockResolvedValue(null),
    socialPosts: vi.fn().mockResolvedValue([]),
    socialRadar: vi.fn().mockResolvedValue([]),
  },
}))

const socialStatus: SocialStatus = {
  enabled: true,
  collector_configured: true,
  agent_configured: false,
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

const authors: Author[] = [
  {
    id: 1,
    name: '浅野一二〇',
    avatar_url: 'https://pbs.twimg.com/asano.jpg',
    x_handle: 'asano_inio',
    x_display_name: 'Inio Asano',
    x_last_synced_at: '2026-07-19T03:55:00Z',
    x_sync_error: null,
    created_at: '2026-07-01T00:00:00Z',
    last_checked_at: '2026-07-19T03:55:00Z',
    work_count: 2,
  },
  {
    id: 2,
    name: 'mignon',
    avatar_url: null,
    x_handle: 'mignon',
    x_display_name: null,
    x_last_synced_at: null,
    x_sync_error: '网络连接失败',
    created_at: '2026-07-02T00:00:00Z',
    last_checked_at: null,
    work_count: 1,
  },
]

const works = [
  {
    id: 10,
    title: '晚安，布布',
    cover_url: 'https://example.com/cover.jpg',
    authors: [{ id: 1, name: '浅野一二〇' }],
    latest_source_at: '2026-07-19T03:55:00Z',
    first_source_at: '2026-07-01T00:00:00Z',
  },
] as WorkGroup[]

afterEach(cleanup)

describe('AuthorDirectory', () => {
  it('brings the selected author details into view on mobile', () => {
    const scrollIntoView = vi.fn()
    const originalMatchMedia = window.matchMedia
    window.matchMedia = vi.fn().mockReturnValue({ matches: true })
    const { container } = render(
      <AuthorDirectory
        authors={authors}
        works={works}
        socialStatus={socialStatus}
        previewAuthorId={1}
        onPreviewAuthor={vi.fn()}
        onOpenAuthorWorks={vi.fn()}
        onOpenWork={vi.fn()}
      />,
    )
    const inspector = container.querySelector('.author-inspector')!
    inspector.scrollIntoView = scrollIntoView
    fireEvent.click(screen.getByRole('option', { name: /mignon/ }))
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' })
    window.matchMedia = originalMatchMedia
  })

  it('requires explicit confirmation before adding a suggested source name', async () => {
    const onGetQueryName = vi.fn().mockImplementation(async (_id: number, provider: string) => ({
      provider, query_name: provider === 'nhentai' ? 'asano' : '浅野一二〇',
      origin: 'inferred', support: 4,
    }))
    const onUpdateAuthor = vi.fn().mockResolvedValue(undefined)
    render(
      <AuthorDirectory
        authors={authors}
        works={works}
        socialStatus={socialStatus}
        previewAuthorId={1}
        onPreviewAuthor={vi.fn()}
        onOpenAuthorWorks={vi.fn()}
        onOpenWork={vi.fn()}
        onGetQueryName={onGetQueryName}
        onUpdateAuthor={onUpdateAuthor}
      />,
    )
    expect(await screen.findByText(/nhentai：asano/)).toBeInTheDocument()
    expect(onUpdateAuthor).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认是别名' }))
    await waitFor(() => expect(onUpdateAuthor).toHaveBeenCalledWith(1, { name: '浅野一二〇', aliases: ['asano'] }))
  })

  it('filters authors, opens representative work, switches integrated radar and accounts tabs', async () => {
    const onPreviewAuthor = vi.fn()
    const onOpenAuthorWorks = vi.fn()
    const onOpenWork = vi.fn()
    const { rerender } = render(
      <AuthorDirectory
        authors={authors}
        works={works}
        socialStatus={socialStatus}
        previewAuthorId={1}
        onPreviewAuthor={onPreviewAuthor}
        onOpenAuthorWorks={onOpenAuthorWorks}
        onOpenWork={onOpenWork}
      />,
    )

    expect(screen.getByRole('heading', { name: '浅野一二〇' })).toBeInTheDocument()
    fireEvent.click(screen.getByText('晚安，布布').closest('button')!)
    expect(onOpenWork).toHaveBeenCalledWith(10)

    fireEvent.click(screen.getByRole('option', { name: /mignon/ }))
    expect(onPreviewAuthor).toHaveBeenCalledWith(2)

    rerender(
      <AuthorDirectory
        authors={authors}
        works={works}
        socialStatus={socialStatus}
        previewAuthorId={2}
        onPreviewAuthor={onPreviewAuthor}
        onOpenAuthorWorks={onOpenAuthorWorks}
        onOpenWork={onOpenWork}
      />,
    )
    expect(screen.getAllByText('同步异常')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '查看 1 部作品' }))
    expect(onOpenAuthorWorks).toHaveBeenCalledWith(2)

    fireEvent.click(screen.getByRole('button', { name: '动态与情报' }))
    expect(await screen.findByText('最近 7 天摘要')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '账号与设置' }))
    expect(await screen.findByText('mignon 的 X 账号')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('搜索作者名称、别名或 @handle'), { target: { value: 'asano' } })
    expect(screen.getByRole('option', { name: /浅野一二〇/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /mignon/ })).not.toBeInTheDocument()
  })
})
