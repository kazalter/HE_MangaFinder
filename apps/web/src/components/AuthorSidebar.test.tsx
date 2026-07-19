import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthorSidebar } from './AuthorSidebar'

const authors = [
  { id: 1, name: '浅野一二〇', avatar_url: null, x_handle: 'asano_inio', x_display_name: null, x_last_synced_at: null, x_sync_error: null, created_at: '', last_checked_at: null, work_count: 12 },
  { id: 2, name: '押见修造', avatar_url: null, x_handle: null, x_display_name: null, x_last_synced_at: null, x_sync_error: null, created_at: '', last_checked_at: null, work_count: 8 },
  { id: 3, name: 'mignon', avatar_url: 'https://pbs.twimg.com/profile_images/mignon.jpg', x_handle: 'mignon', x_display_name: 'mignon', x_last_synced_at: null, x_sync_error: null, created_at: '', last_checked_at: null, work_count: 5 },
]

const sources = [{ name: 'mangadex', display_name: 'MangaDex', capabilities: ['search'] }]

function renderSidebar(overrides: Partial<Parameters<typeof AuthorSidebar>[0]> = {}) {
  const props = {
    authors,
    sources,
    selectedId: null,
    libraryMode: 'works' as const,
    busy: false,
    onSelect: vi.fn(),
    onModeChange: vi.fn(),
    onAdd: vi.fn().mockResolvedValue(undefined),
    onRefresh: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
    onOpenSettings: vi.fn(),
    onOpenRadar: vi.fn(),
    ...overrides,
  }
  return { ...render(<AuthorSidebar {...props} />), props }
}

describe('AuthorSidebar', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === '(max-width: 800px)' ? false : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
  })

  afterEach(cleanup)

  it('filters the author list and selects an author', () => {
    const { props } = renderSidebar()

    fireEvent.change(screen.getByLabelText('搜索作者'), { target: { value: 'mignon' } })
    expect(screen.getByText('搜索结果')).toBeInTheDocument()
    expect(screen.getByText('mignon')).toBeInTheDocument()
    expect(screen.queryByText('浅野一二〇')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('mignon').closest('button')!)
    expect(props.onSelect).toHaveBeenCalledWith(3)
  })

  it('uses the confirmed X account avatar when the author has one', () => {
    const { container } = renderSidebar()

    expect(container.querySelector('img[src="https://pbs.twimg.com/profile_images/mignon.jpg"]')).toBeInTheDocument()
    expect(screen.getAllByText('浅').length).toBeGreaterThan(0)
  })

  it('opens the author directory from the author tab', () => {
    const { props } = renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: '作者 3' }))
    expect(props.onModeChange).toHaveBeenCalledWith('authors')
  })

  it('persists the collapsed state and opens again from the compact rail', () => {
    renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: '收起资料库导航' }))
    expect(screen.getByLabelText('资料库导航')).toHaveClass('collapsed')
    expect(window.localStorage.getItem('mangafinder:library-sidebar-expanded')).toBe('false')

    fireEvent.click(screen.getByRole('button', { name: '展开资料库导航' }))
    expect(screen.getByLabelText('资料库导航')).toHaveClass('expanded')
  })

  it('submits the expandable add-author form', async () => {
    const onAdd = vi.fn().mockResolvedValue(undefined)
    renderSidebar({ onAdd })

    fireEvent.click(screen.getByRole('button', { name: '添加作者' }))
    fireEvent.change(screen.getByPlaceholderText('输入要订阅的作者名'), { target: { value: '  新作者  ' } })
    fireEvent.click(screen.getByRole('button', { name: '确认添加' }))

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith('新作者'))
    expect(screen.queryByPlaceholderText('输入要订阅的作者名')).not.toBeInTheDocument()
  })

  it('opens the mobile drawer and closes it with Escape', () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === '(max-width: 800px)',
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
    renderSidebar()

    expect(screen.getByLabelText('资料库导航')).toHaveAttribute('aria-hidden', 'true')
    fireEvent.click(screen.getByRole('button', { name: '打开资料库导航' }))
    expect(screen.getByLabelText('资料库导航')).toHaveAttribute('aria-hidden', 'false')
    expect(document.body).toHaveClass('sidebar-drawer-open')

    fireEvent.click(screen.getAllByRole('button', { name: '关闭资料库导航' })[1])
    expect(screen.getByLabelText('资料库导航')).toHaveAttribute('aria-hidden', 'true')

    fireEvent.click(screen.getByRole('button', { name: '打开资料库导航' }))

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByLabelText('资料库导航')).toHaveAttribute('aria-hidden', 'true')
    expect(document.body).not.toHaveClass('sidebar-drawer-open')
  })

  it('persists pinned authors', async () => {
    renderSidebar()

    fireEvent.click(screen.getAllByRole('button', { name: '取消置顶 浅野一二〇' })[0])
    await waitFor(() => expect(window.localStorage.getItem('mangafinder:pinned-authors')).toBe('[2,3]'))
  })
})
