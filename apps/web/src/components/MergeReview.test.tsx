import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { MergeSuggestion, WorkGroup } from '../types'
import { MergeReview } from './MergeReview'

function group(id: number, title: string, provider: string): WorkGroup {
  return {
    id,
    title,
    description: null,
    cover_url: `https://example.test/${id}.jpg`,
    status: 'completed',
    year: 2026,
    language: 'ja',
    tags: [],
    first_source_at: '2025-07-13T00:00:00Z', latest_source_at: '2026-07-13T00:00:00Z',
    edition_count: 2,
    providers: [provider],
    authors: [{ id: 1, name: '测试作者' }],
  }
}

describe('MergeReview', () => {
  it('shows both covers and opens the same group detail flow', () => {
    const source = group(11, '作品 A', 'wnacg')
    const target = group(22, '作品 B', 'nhentai')
    const suggestion: MergeSuggestion = {
      id: 1,
      source_group_id: source.id,
      source_title: source.title,
      target_group_id: target.id,
      target_title: target.title,
      source_group: source,
      target_group: target,
      confidence: 0.82,
      reasons: ['标题相似'],
      status: 'pending',
      agent_review: null,
      hard_conflicts: [],
      soft_conflicts: [],
      conflict_details: [],
      core_title_similarity: 0.82,
      cover_hash_distance: 4,
      cover_match_mode: 'crop',
      cover_legacy_distance: 28,
      source_identity_titles: ['作品 A'],
      target_identity_titles: ['作品 B'],
      shared_context: [],
    }
    const onOpenGroup = vi.fn()

    render(
      <MergeReview
        suggestions={[suggestion]}
        agentStatus={null}
        busy={false}
        onRunAgent={() => undefined}
        onOpenGroup={onOpenGroup}
        onAccept={() => undefined}
        onReject={() => undefined}
        onClose={() => undefined}
      />,
    )

    expect(screen.getByAltText('作品 A 封面')).toHaveAttribute('src', source.cover_url)
    expect(screen.getByText('wnacg')).toBeInTheDocument()
    expect(screen.getByText('nhentai')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看《作品 B》的作品详情' }))
    expect(onOpenGroup).toHaveBeenCalledWith(22)
  })
})
