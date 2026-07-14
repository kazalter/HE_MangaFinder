import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkCard } from './WorkCard'

describe('WorkCard', () => {
  it('renders grouped metadata and source badges', () => {
    render(<WorkCard onOpen={() => undefined} work={{
      id: 1, title: '测试漫画', description: '简介', cover_url: null,
      status: 'ongoing', year: 2026, language: 'ja', tags: ['Drama'],
      latest_source_at: '2026-01-01T00:00:00Z', edition_count: 3,
      providers: ['mangadex', 'wnacg'],
    }} />)
    expect(screen.getByText('测试漫画')).toBeInTheDocument()
    expect(screen.getByText('连载中')).toBeInTheDocument()
    expect(screen.getByText('3 个版本')).toBeInTheDocument()
    expect(screen.getByText('mangadex')).toBeInTheDocument()
    expect(screen.getByText('wnacg')).toBeInTheDocument()
  })

  it('shows a readable fallback when a cached cover fails', () => {
    render(<WorkCard onOpen={() => undefined} work={{
      id: 2, title: '封面测试', description: null, cover_url: '/api/works/2/cover?v=1',
      status: null, year: null, language: null, tags: [], latest_source_at: null,
      edition_count: 1, providers: ['nhentai'],
    }} />)

    fireEvent.error(screen.getByAltText('封面测试 封面'))

    expect(screen.getByText('封面加载失败')).toBeInTheDocument()
  })
})
