import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WorkGroup } from '../types'
import { WorkListRow } from './WorkListRow'

const work: WorkGroup = {
  id: 8,
  title: '悬浮封面测试',
  description: null,
  cover_url: '/api/works/8/cover?v=1',
  status: 'completed',
  year: 2026,
  language: 'ja',
  tags: ['Drama'],
  first_source_at: '2026-01-01T00:00:00Z',
  latest_source_at: '2026-01-02T00:00:00Z',
  edition_count: 2,
  providers: ['nhentai'],
  authors: [{ id: 1, name: '测试作者' }],
}

afterEach(cleanup)

describe('WorkListRow', () => {
  it('expands the row when the pointer rests anywhere on it', async () => {
    render(<WorkListRow work={work} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')

    fireEvent.pointerEnter(row, { pointerType: 'mouse' })

    await waitFor(() => expect(row).toHaveClass('preview-active'))
    expect(screen.getByText('悬浮封面测试')).toBeVisible()

    fireEvent.pointerLeave(row, { pointerType: 'mouse' })
    await waitFor(() => expect(row).not.toHaveClass('preview-active'))
  })

  it('does not expand when the work has no cover', () => {
    render(<WorkListRow work={{ ...work, cover_url: null }} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')

    fireEvent.pointerEnter(row, { pointerType: 'mouse' })

    expect(row).not.toHaveClass('preview-active')
  })

  it('hands the expansion directly to the next row without a collapsing gap', async () => {
    render(<>
      <WorkListRow work={work} onOpen={vi.fn()} />
      <WorkListRow work={{ ...work, id: 9, title: '下一部作品' }} onOpen={vi.fn()} />
    </>)
    const [firstRow, secondRow] = screen.getAllByRole('button')

    fireEvent.pointerEnter(firstRow, { pointerType: 'mouse', clientY: 240 })
    await waitFor(() => expect(firstRow).toHaveClass('preview-active'))

    fireEvent.pointerLeave(firstRow, { pointerType: 'mouse', clientY: 300 })
    fireEvent.pointerEnter(secondRow, { pointerType: 'mouse', clientY: 300 })
    expect(firstRow).toHaveClass('preview-active')

    await waitFor(() => expect(secondRow).toHaveClass('preview-active'))
    expect(firstRow).not.toHaveClass('preview-active')
  })

  it('reopens the hovered row after scrolling stops', async () => {
    render(<WorkListRow work={work} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')

    fireEvent.pointerEnter(row, { pointerType: 'mouse' })
    await waitFor(() => expect(row).toHaveClass('preview-active'))

    fireEvent.scroll(window)
    expect(row).not.toHaveClass('preview-active')

    await waitFor(() => expect(row).toHaveClass('preview-active'))
  })

  it('closes the preview before opening the work detail', async () => {
    const onOpen = vi.fn()
    render(<WorkListRow work={work} onOpen={onOpen} />)
    const row = screen.getByRole('button')

    fireEvent.pointerEnter(row, { pointerType: 'mouse' })
    await waitFor(() => expect(row).toHaveClass('preview-active'))
    fireEvent.click(row)

    expect(row).not.toHaveClass('preview-active')
    expect(onOpen).toHaveBeenCalledWith(work)
  })
})
