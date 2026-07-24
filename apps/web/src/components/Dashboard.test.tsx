import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Dashboard } from './Dashboard'

const data = {
  period_days: 30,
  summary: {
    authors: 3, works: 12, editions: 18, new_works: 2, new_editions: 1,
    unread_discoveries: 1, pending_merges: 2, pending_signals: 3,
    failed_provider_scans: 1, incomplete_works: 4,
  },
  trend: [{ date: '2026-07-24', works: 2, editions: 1 }],
  source_health: [{
    provider: 'wnacg', scans: 4, succeeded: 3, failed: 1, success_rate: 0.75,
    new_items: 3, average_duration_ms: 800, last_success_at: '2026-07-24T10:00:00Z',
    last_error: '暂时不可达',
  }],
  upcoming: [{
    id: 4, author_id: 2, author_name: '作者乙', title: '八月新刊', kind: 'new_release',
    status: 'confirmed', release_date: '2026-08-10T00:00:00Z', event_code: 'C108',
    booth: '東A-01', linked_group_id: 9,
  }],
}

const events = [{
  id: 7, author_id: 1, author_name: '作者甲', group_id: 5, work_id: 6,
  provider: 'wnacg', external_id: '100', event_type: 'work_discovered' as const,
  title: '新发现作品', cover_url: null, state: 'unread' as const,
  created_at: '2026-07-24T10:00:00Z', read_at: null,
}]

describe('Dashboard', () => {
  afterEach(cleanup)

  it('shows actionable statistics and updates inbox items', () => {
    const onSetEventState = vi.fn()
    const onOpenWork = vi.fn()
    render(<Dashboard
      data={data}
      events={events}
      busy={false}
      onReload={vi.fn()}
      onOpenWork={onOpenWork}
      onOpenRadar={vi.fn()}
      onSetEventState={onSetEventState}
      onReadAll={vi.fn()}
      onOpenReviews={vi.fn()}
    />)

    expect(screen.getByRole('heading', { name: '资料库总览' })).toBeInTheDocument()
    expect(screen.getByText('新发现作品')).toBeInTheDocument()
    expect(screen.getByText('75%')).toBeInTheDocument()
    expect(screen.getByText('八月新刊')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '标为已读' }))
    expect(onSetEventState).toHaveBeenCalledWith(7, 'read')
    fireEvent.click(screen.getByRole('button', { name: '打开《新发现作品》' }))
    expect(onOpenWork).toHaveBeenCalledWith(5)
  })
})
