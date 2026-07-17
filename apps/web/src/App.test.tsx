import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const { works } = vi.hoisted(() => ({
  works: [{
    id: 1,
    title: '作品甲',
    description: '简介甲',
    cover_url: null,
    status: 'ongoing',
    year: 2026,
    language: 'ja',
    tags: ['Drama'],
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
    workGroups: vi.fn().mockResolvedValue(works),
    jobs: vi.fn().mockResolvedValue([]),
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
  beforeEach(() => window.localStorage.clear())

  it('switches display mode, filters works, and groups by author', async () => {
    render(<App />)
    expect(await screen.findByText('作品甲')).toBeInTheDocument()

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
})
