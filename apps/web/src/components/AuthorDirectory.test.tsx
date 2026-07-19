import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Author, WorkGroup } from '../types'
import { AuthorDirectory } from './AuthorDirectory'

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
  it('filters authors, previews a row, and opens its works', () => {
    const onPreviewAuthor = vi.fn()
    const onOpenAuthor = vi.fn()
    const { rerender } = render(
      <AuthorDirectory
        authors={authors}
        works={works}
        previewAuthorId={1}
        onPreviewAuthor={onPreviewAuthor}
        onOpenAuthor={onOpenAuthor}
      />,
    )

    expect(screen.getByRole('heading', { name: '浅野一二〇' })).toBeInTheDocument()
    expect(screen.getByText('晚安，布布')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('option', { name: /mignon/ }))
    expect(onPreviewAuthor).toHaveBeenCalledWith(2)

    rerender(
      <AuthorDirectory
        authors={authors}
        works={works}
        previewAuthorId={2}
        onPreviewAuthor={onPreviewAuthor}
        onOpenAuthor={onOpenAuthor}
      />,
    )
    expect(screen.getAllByText('同步异常')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '查看 1 部作品' }))
    expect(onOpenAuthor).toHaveBeenCalledWith(2)

    fireEvent.change(screen.getByPlaceholderText('搜索作者名称或 @handle'), { target: { value: 'asano' } })
    expect(screen.getByRole('option', { name: /浅野一二〇/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /mignon/ })).not.toBeInTheDocument()
  })
})
