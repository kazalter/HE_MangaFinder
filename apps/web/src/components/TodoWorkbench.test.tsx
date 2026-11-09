import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TodoWorkbench } from './TodoWorkbench'

const mocks = vi.hoisted(() => ({ discoveryInbox: vi.fn(), mergeSuggestions: vi.fn(), plannedMatches: vi.fn(), failedDownloads: vi.fn(), updateDiscoveryEvent: vi.fn(), updateSelectedDiscoveryEvents: vi.fn(), acceptSuggestion: vi.fn(), rejectSuggestion: vi.fn(), linkPlannedMatch: vi.fn() }))
vi.mock('../lib/api', () => ({ api: mocks }))
const event = (id: number) => ({ id, title: `发现 ${id}`, author_name: '作者', event_type: 'work_discovered', provider: 'wnacg', group_id: 4, cover_url: null, created_at: '2026-11-01T00:00:00Z' })

beforeEach(() => {
  vi.resetAllMocks()
  mocks.discoveryInbox.mockResolvedValue([event(1), event(2)])
  mocks.mergeSuggestions.mockResolvedValue([])
  mocks.plannedMatches.mockResolvedValue([])
  mocks.failedDownloads.mockResolvedValue({ items: [], has_more: false })
  mocks.updateDiscoveryEvent.mockResolvedValue(undefined)
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('待办工作台', () => {
  it('batch marks only selected discoveries and reports changed state', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocks.updateSelectedDiscoveryEvents.mockResolvedValue({ changed: 1 })
    render(<TodoWorkbench works={[]} onOpenWork={vi.fn()} onOpenReviews={vi.fn()} onChanged={vi.fn()} />)
    await screen.findByText('发现 1')
    fireEvent.click(screen.getByLabelText('选择 发现 1'))
    fireEvent.click(screen.getByLabelText('选择 发现 2'))
    fireEvent.click(screen.getByRole('button', { name: '标为已读' }))
    expect(await screen.findByText(/已处理 1 条，1 条状态已变化或不存在/)).toBeInTheDocument()
    expect(mocks.updateSelectedDiscoveryEvents).toHaveBeenCalledWith([1, 2], 'read')
  })

  it('limits the overview selection to visible discoveries', async () => {
    mocks.discoveryInbox.mockResolvedValue(Array.from({ length: 7 }, (_, i) => event(i + 1)))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocks.updateSelectedDiscoveryEvents.mockResolvedValue({ changed: 5 })
    render(<TodoWorkbench works={[]} onOpenWork={vi.fn()} onOpenReviews={vi.fn()} onChanged={vi.fn()} />)
    await screen.findByText('发现 1')
    expect(screen.queryByText('发现 6')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('全选当前展示'))
    fireEvent.click(screen.getByRole('button', { name: '标为已读' }))
    await waitFor(() => expect(mocks.updateSelectedDiscoveryEvents).toHaveBeenCalledWith([1, 2, 3, 4, 5], 'read'))
  })

  it('keeps other categories usable when one fails and opens failed works', async () => {
    mocks.plannedMatches.mockRejectedValue(new Error('offline'))
    mocks.failedDownloads.mockResolvedValue({ items: [{ job: { id: 1, payload: { provider: 'wnacg', work_id: 7 }, created_at: '2026-11-01', error: '连接失败', attempts: 3 }, work_title: '重试作品', group_id: 9 }], has_more: false })
    const open = vi.fn()
    render(<TodoWorkbench works={[]} onOpenWork={open} onOpenReviews={vi.fn()} onChanged={vi.fn()} />)
    expect(await screen.findByText('待发布命中加载失败，请刷新重试。')).toBeInTheDocument()
    const failure = screen.getByRole('region', { name: '下载失败' })
    fireEvent.click(within(failure).getByRole('button', { name: '打开作品重新下载' }))
    expect(open).toHaveBeenCalledWith(9)
    fireEvent.click(screen.getByRole('button', { name: /下载失败/ }))
    expect(screen.queryByRole('region', { name: '新发现' })).not.toBeInTheDocument()
  })
})
