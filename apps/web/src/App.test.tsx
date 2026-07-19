import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const { jobsMock, workGroupsMock, works } = vi.hoisted(() => ({
  jobsMock: vi.fn(),
  workGroupsMock: vi.fn(),
  works: [{
    id: 1,
    title: '作品甲',
    description: '简介甲',
    cover_url: null,
    status: 'ongoing',
    year: 2026,
    language: 'ja',
    tags: ['Drama'],
    first_source_at: '2024-01-01T00:00:00Z',
    latest_source_at: '2026-07-15T00:00:00Z',
    edition_count: 2,
    providers: ['mangadex'],
    authors: [{ id: 1, name: '作者甲' }],
  }, {
    id: 2,
    title: '作品乙',
    description: null,
    cover_url: null,
    status: 'completed',
    year: 2025,
    language: 'zh-hans',
    tags: [],
    first_source_at: '2025-01-01T00:00:00Z',
    latest_source_at: '2026-07-14T00:00:00Z',
    edition_count: 1,
    providers: ['wnacg'],
    authors: [{ id: 2, name: '作者乙' }],
  }],
}))

vi.mock('./lib/api', () => ({
  api: {
    authors: vi.fn().mockResolvedValue([
      { id: 1, name: '作者甲', created_at: '', last_checked_at: null, work_count: 1 },
      { id: 2, name: '作者乙', created_at: '', last_checked_at: null, work_count: 1 },
    ]),
    workGroups: workGroupsMock,
    jobs: jobsMock,
    sources: vi.fn().mockResolvedValue([]),
    mergeSuggestions: vi.fn().mockResolvedValue([]),
    agentStatus: vi.fn().mockResolvedValue(null),
    socialStatus: vi.fn().mockResolvedValue({
      enabled: false,
      collector_configured: false,
      agent_configured: false,
      qq_configured: false,
      auto_confirm_threshold: 0.92,
      candidate_threshold: 0.6,
      pending_count: 0,
      unread_count: 0,
      daily_digest_enabled: false,
      daily_digest_hour: 20,
      daily_digest_timezone: 'Asia/Shanghai',
    }),
    socialRadar: vi.fn().mockResolvedValue([]),
  },
}))

describe('catalog views', () => {
  afterEach(cleanup)

  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    jobsMock.mockClear()
    jobsMock.mockResolvedValue([])
    workGroupsMock.mockReset()
    workGroupsMock.mockResolvedValue(works)
  })

  it('switches display mode, filters works, and groups by author', async () => {
    render(<App />)
    expect(await screen.findByText('作品甲')).toBeInTheDocument()
    expect(screen.getByLabelText('排序')).toHaveValue('first')
    expect(document.querySelector('.work-grid h3')).toHaveTextContent('作品乙')

    fireEvent.click(screen.getByRole('button', { name: '封面墙' }))
    expect(screen.getByRole('button', { name: '查看《作品甲》' })).toBeInTheDocument()
    expect(window.localStorage.getItem('mangafinder:view')).toBe('covers')

    fireEvent.change(screen.getByLabelText('筛选'), { target: { value: 'multi' } })
    expect(screen.getByText('作品甲')).toBeInTheDocument()
    expect(screen.queryByText('作品乙')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('筛选'), { target: { value: 'all' } })
    fireEvent.change(screen.getByLabelText('分组'), { target: { value: 'author' } })
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: '作者甲' })).toBeInTheDocument()
      expect(screen.getByRole('heading', { name: '作者乙' })).toBeInTheDocument()
    })
  })

  it('restores the selected author after a page reload', async () => {
    const first = render(<App />)
    await screen.findByRole('heading', { name: '作品总览' })

    fireEvent.click(screen.getAllByText('作者乙')[0].closest('button')!)
    await waitFor(() => {
      expect(window.localStorage.getItem('mangafinder:selected-author')).toBe('2')
      expect(workGroupsMock).toHaveBeenCalledWith(2)
    })
    first.unmount()
    workGroupsMock.mockClear()

    render(<App />)

    expect(await screen.findByRole('heading', { name: '作者乙' })).toBeInTheDocument()
    expect(workGroupsMock).toHaveBeenCalledWith(2)
  })

  it('explains, timestamps, and dismisses a failed task without hiding newer failures', async () => {
    jobsMock.mockResolvedValue([{
      id: 58,
      kind: 'social_sync_account',
      payload: { account_id: 1 },
      status: 'failed',
      attempts: 3,
      error: '无法连接 X 采集器：All connection attempts failed',
      created_at: '2026-07-17T02:19:00Z',
      started_at: '2026-07-17T02:19:19Z',
      finished_at: '2026-07-17T02:19:20Z',
      next_attempt_at: null,
    }])
    const first = render(<App />)

    expect(await screen.findByText('X 动态同步失败')).toBeInTheDocument()
    expect(screen.getByText(/采集器在服务启动时尚未就绪/)).toBeInTheDocument()
    expect(screen.getByText(/已尝试 3 次/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭任务失败提示' }))
    expect(screen.queryByText('X 动态同步失败')).not.toBeInTheDocument()
    expect(window.sessionStorage.getItem('mangafinder:dismissed-job-through')).toBe('58')
    first.unmount()

    jobsMock.mockResolvedValue([{
      id: 59,
      kind: 'download_chapter',
      payload: {},
      status: 'failed',
      attempts: 3,
      error: '下载源暂时不可用',
      created_at: '2026-07-17T03:00:00Z',
      started_at: '2026-07-17T03:00:01Z',
      finished_at: '2026-07-17T03:00:02Z',
      next_attempt_at: null,
    }])
    render(<App />)
    expect(await screen.findByText('章节下载失败')).toBeInTheDocument()
  })

  it('stops showing an old failure after the same task subject recovers', async () => {
    jobsMock.mockResolvedValue([{
      id: 60,
      kind: 'social_sync_account',
      payload: { account_id: 1 },
      status: 'succeeded',
      attempts: 1,
      error: null,
      created_at: '2026-07-17T03:00:00Z',
      started_at: '2026-07-17T03:00:01Z',
      finished_at: '2026-07-17T03:00:02Z',
      next_attempt_at: null,
    }, {
      id: 58,
      kind: 'social_sync_account',
      payload: { account_id: 1 },
      status: 'failed',
      attempts: 3,
      error: '无法连接 X 采集器：All connection attempts failed',
      created_at: '2026-07-17T02:19:00Z',
      started_at: '2026-07-17T02:19:19Z',
      finished_at: '2026-07-17T02:19:20Z',
      next_attempt_at: null,
    }])
    render(<App />)

    expect(await screen.findByRole('heading', { name: '作品总览' })).toBeInTheDocument()
    expect(screen.queryByText('X 动态同步失败')).not.toBeInTheDocument()
  })
})
