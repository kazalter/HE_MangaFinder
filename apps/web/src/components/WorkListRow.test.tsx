import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WorkGroup } from '../types'
import { WorkListRow } from './WorkListRow'

const work: WorkGroup = {
  id: 8,
  title: '悬浮封面测试',
  description: '这是一段简介内容 [链接](https://example.com)',
  cover_url: '/api/works/8/cover?v=1',
  status: 'completed',
  year: 2026,
  language: 'ja',
  tags: ['Drama', 'Romance', 'Slice of Life', 'School', 'Comedy', 'Fantasy', 'ExtraTag'],
  first_source_at: '2026-01-01T00:00:00Z',
  latest_source_at: '2026-01-02T00:00:00Z',
  edition_count: 2,
  providers: ['nhentai', 'wnacg'],
  authors: [{ id: 1, name: '测试作者' }],
}

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe('WorkListRow', () => {
  it('ignores text-area hover and opens floating popover after resting on the cover thumbnail', () => {
    vi.useFakeTimers()
    render(<WorkListRow work={work} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')
    const trigger = screen.getByTestId('list-cover-trigger')

    // Resting on the text columns should never pop open a preview.
    fireEvent.pointerEnter(within(row).getByText('悬浮封面测试'), { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(400) })
    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()

    // Brief sweep across the cover (< 240ms) should not trigger accidental popups.
    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(150) })
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()

    // Dwelling past the 240ms threshold opens the floating preview in document.body.
    act(() => { vi.advanceTimersByTime(100) })
    expect(row).toHaveClass('preview-active')
    const popover = screen.getByTestId('work-list-preview')
    expect(popover).toBeVisible()
    expect(row).not.toContainElement(popover)
    expect(within(popover).getByText('悬浮封面测试')).toBeVisible()
    expect(within(popover).getByText('这是一段简介内容')).toBeVisible()
    expect(within(popover).getByText('+1')).toBeVisible()

    // Leaving the cover dismisses the floating preview after the close grace window.
    fireEvent.pointerLeave(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(120) })
    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()
  })

  it('dismisses the floating preview immediately when moving onto row text to select', () => {
    vi.useFakeTimers()
    render(<WorkListRow work={work} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')
    const trigger = screen.getByTestId('list-cover-trigger')

    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(240) })
    expect(screen.getByTestId('work-list-preview')).toBeVisible()

    fireEvent.pointerMove(within(row).getByText('悬浮封面测试'), { pointerType: 'mouse' })
    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()
  })

  it('does not open when the work has no cover', () => {
    vi.useFakeTimers()
    render(<WorkListRow work={{ ...work, cover_url: null }} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')
    const trigger = screen.getByTestId('list-cover-trigger')

    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(300) })

    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()
  })

  it('hands the floating preview directly to the next cover thumbnail without blinking shut', () => {
    vi.useFakeTimers()
    render(<>
      <WorkListRow work={work} onOpen={vi.fn()} />
      <WorkListRow work={{ ...work, id: 9, title: '下一部作品' }} onOpen={vi.fn()} />
    </>)
    const [firstRow, secondRow] = screen.getAllByRole('button')
    const [firstTrigger, secondTrigger] = screen.getAllByTestId('list-cover-trigger')

    fireEvent.pointerEnter(firstTrigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(240) })
    expect(firstRow).toHaveClass('preview-active')

    fireEvent.pointerLeave(firstTrigger, { pointerType: 'mouse' })
    fireEvent.pointerEnter(secondTrigger, { pointerType: 'mouse' })
    expect(firstRow).toHaveClass('preview-active')

    act(() => { vi.advanceTimersByTime(90) })
    expect(secondRow).toHaveClass('preview-active')
    expect(firstRow).not.toHaveClass('preview-active')
    expect(within(screen.getByTestId('work-list-preview')).getByText('下一部作品')).toBeVisible()
  })

  it('closes during scroll and reopens the hovered cover after scrolling stops', () => {
    vi.useFakeTimers()
    render(<WorkListRow work={work} onOpen={vi.fn()} />)
    const row = screen.getByRole('button')
    const trigger = screen.getByTestId('list-cover-trigger')

    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(240) })
    expect(row).toHaveClass('preview-active')

    fireEvent.scroll(window)
    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(180 + 240) })
    expect(row).toHaveClass('preview-active')
    expect(screen.getByTestId('work-list-preview')).toBeVisible()
  })

  it('closes the preview before opening the work detail', () => {
    vi.useFakeTimers()
    const onOpen = vi.fn()
    render(<WorkListRow work={work} onOpen={onOpen} />)
    const row = screen.getByRole('button')
    const trigger = screen.getByTestId('list-cover-trigger')

    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(240) })
    expect(row).toHaveClass('preview-active')

    fireEvent.click(row)

    expect(row).not.toHaveClass('preview-active')
    expect(screen.queryByTestId('work-list-preview')).not.toBeInTheDocument()
    expect(onOpen).toHaveBeenCalledWith(work)
  })

  it('detaches global scroll listeners once all list rows unmount', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<WorkListRow work={work} onOpen={vi.fn()} />)
    fireEvent.pointerEnter(screen.getByTestId('list-cover-trigger'), { pointerType: 'mouse' })

    unmount()

    expect(removeSpy).toHaveBeenCalledWith('scroll', expect.any(Function), { capture: true })
    expect(removeSpy).toHaveBeenCalledWith('wheel', expect.any(Function))
    expect(removeSpy).toHaveBeenCalledWith('resize', expect.any(Function))
    removeSpy.mockRestore()
  })
})
