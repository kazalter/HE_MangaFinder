import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Workspace } from './Workspace'

const mocks = vi.hoisted(() => ({
  discoveryInbox: vi.fn(),
  plannedMatches: vi.fn(),
  failedDownloads: vi.fn(),
  updateDiscoveryEvent: vi.fn(),
  updateSelectedDiscoveryEvents: vi.fn(),
  updateAllDiscoveryEvents: vi.fn(),
  acceptSuggestion: vi.fn(),
  rejectSuggestion: vi.fn(),
  linkPlannedMatch: vi.fn(),
  catalogOperations: vi.fn(),
  undoCatalogOperation: vi.fn(),
}))

vi.mock('../lib/api', () => ({ api: mocks }))

const dashboardData = {
  period_days: 30,
  summary: {
    authors: 3, works: 12, editions: 18, new_works: 2, new_editions: 1,
    unread_discoveries: 2, pending_merges: 1, pending_signals: 3,
    failed_provider_scans: 1, incomplete_works: 4,
  },
  trend: [{ date: '2026-07-24', works: 2, editions: 1 }],
  source_health: [{
    provider: 'wnacg', scans: 4, succeeded: 3, failed: 1, success_rate: 0.75,
    new_items: 3, average_duration_ms: 800, last_success_at: '2026-07-24T10:00:00Z',
    last_error: '暂时不可达', current_failed_authors: 1, current_checked_authors: 2,
    failure_categories: { timeout: 1 }, retry_paused_until: '2099-01-01T00:00:00Z',
  }],
  upcoming: [{
    id: 4, author_id: 2, author_name: '作者乙', title: '八月新刊', kind: 'new_release',
    status: 'confirmed', release_date: '2026-08-10T00:00:00Z', event_code: 'C108',
    booth: '東A-01', linked_group_id: 9,
  }],
}

const makeEvent = (id: number) => ({
  id,
  author_id: 1,
  author_name: '作者甲',
  group_id: 5,
  work_id: 6,
  provider: 'wnacg',
  external_id: String(100 + id),
  event_type: 'work_discovered' as const,
  title: `发现作品 ${id}`,
  cover_url: null,
  state: 'unread' as const,
  created_at: '2026-11-01T00:00:00Z',
  read_at: null,
})

beforeEach(() => {
  vi.resetAllMocks()
  mocks.discoveryInbox.mockResolvedValue([makeEvent(1), makeEvent(2)])
  mocks.plannedMatches.mockResolvedValue([])
  mocks.failedDownloads.mockResolvedValue({ items: [], has_more: false })
  mocks.updateDiscoveryEvent.mockResolvedValue(undefined)
  mocks.catalogOperations.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Workspace', () => {
  it('renders unified workspace, handles single discovery actions, and opens works', async () => {
    const onOpenWork = vi.fn()
    const onChanged = vi.fn().mockResolvedValue(undefined)
    render(
      <Workspace
        tab="discovery"
        onTabChange={vi.fn()}
        data={dashboardData}
        works={[]}
        suggestions={[]}
        seriesCandidates={[]}
        agentStatus={null}
        busy={false}
        onReload={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={onOpenWork}
        onOpenAuthorCenter={vi.fn()}
        onRunAgent={vi.fn()}
        onAcceptMerge={vi.fn()}
        onRejectMerge={vi.fn()}
        onCreateSeries={vi.fn()}
        onChanged={onChanged}
      />,
    )

    expect(screen.getByRole('heading', { name: '工作台' })).toBeInTheDocument()
    expect(await screen.findByText('发现作品 1')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '打开《发现作品 1》' }))
    expect(onOpenWork).toHaveBeenCalledWith(5)

    fireEvent.click(screen.getAllByRole('button', { name: '标为已读' })[0])
    await waitFor(() => expect(mocks.updateDiscoveryEvent).toHaveBeenCalledWith(1, 'read'))
    expect(onChanged).toHaveBeenCalled()
  })

  it('batch marks selected discoveries and reports partial changes', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocks.updateSelectedDiscoveryEvents.mockResolvedValue({ changed: 1 })
    render(
      <Workspace
        tab="discovery"
        onTabChange={vi.fn()}
        data={dashboardData}
        works={[]}
        suggestions={[]}
        seriesCandidates={[]}
        agentStatus={null}
        busy={false}
        onReload={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={vi.fn()}
        onOpenAuthorCenter={vi.fn()}
        onRunAgent={vi.fn()}
        onAcceptMerge={vi.fn()}
        onRejectMerge={vi.fn()}
        onCreateSeries={vi.fn()}
        onChanged={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    await screen.findByText('发现作品 1')
    fireEvent.click(screen.getByLabelText('全选当前展示'))
    fireEvent.click(screen.getByRole('button', { name: '批量标为已读' }))

    expect(await screen.findByText(/已处理 1 条，1 条状态已变化或不存在/)).toBeInTheDocument()
    expect(mocks.updateSelectedDiscoveryEvents).toHaveBeenCalledWith([1, 2], 'read')
  })

  it('shows planned/upcoming signals and health/failure details cleanly', async () => {
    mocks.plannedMatches.mockRejectedValue(new Error('offline'))
    mocks.failedDownloads.mockResolvedValue({
      items: [{
        job: { id: 1, kind: 'download_chapter', payload: { provider: 'wnacg', work_id: 7 }, status: 'failed', created_at: '2026-11-01', started_at: null, finished_at: null, next_attempt_at: null, error: '连接失败', attempts: 3 },
        work_title: '重试作品',
        group_id: 9,
      }],
      has_more: false,
    })
    const onOpenWork = vi.fn()
    const { rerender } = render(
      <Workspace
        tab="planned"
        onTabChange={vi.fn()}
        data={dashboardData}
        works={[]}
        suggestions={[]}
        seriesCandidates={[]}
        agentStatus={null}
        busy={false}
        onReload={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={onOpenWork}
        onOpenAuthorCenter={vi.fn()}
        onRunAgent={vi.fn()}
        onAcceptMerge={vi.fn()}
        onRejectMerge={vi.fn()}
        onCreateSeries={vi.fn()}
        onChanged={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    expect(await screen.findByText('待发布命中加载失败，请刷新重试。')).toBeInTheDocument()
    expect(screen.getByText('八月新刊')).toBeInTheDocument()

    rerender(
      <Workspace
        tab="failures"
        onTabChange={vi.fn()}
        data={dashboardData}
        works={[]}
        suggestions={[]}
        seriesCandidates={[]}
        agentStatus={null}
        busy={false}
        onReload={vi.fn().mockResolvedValue(undefined)}
        onOpenWork={onOpenWork}
        onOpenAuthorCenter={vi.fn()}
        onRunAgent={vi.fn()}
        onAcceptMerge={vi.fn()}
        onRejectMerge={vi.fn()}
        onCreateSeries={vi.fn()}
        onChanged={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    const failuresRegion = screen.getByRole('region', { name: '异常与健康' })
    expect(within(failuresRegion).getByText('75%')).toBeInTheDocument()
    expect(within(failuresRegion).getByText('失败分类（含重试）：超时 1')).toBeInTheDocument()
    fireEvent.click(within(failuresRegion).getByRole('button', { name: '打开作品重新下载' }))
    expect(onOpenWork).toHaveBeenCalledWith(9)
  })
})
