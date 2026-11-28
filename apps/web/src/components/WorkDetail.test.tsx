import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkDetail } from './WorkDetail'

const apiMocks = vi.hoisted(() => ({
  libraryCheck: vi.fn(),
  chapters: vi.fn(),
  downloadChapter: vi.fn(),
}))

vi.mock('../lib/api', () => ({ api: apiMocks }))

const group = {
  id: 10,
  title: '误收录测试',
  description: null,
  cover_url: null,
  status: null,
  year: null,
  language: 'zh-hans',
  tags: [],
  first_source_at: null,
  latest_source_at: '2025-03-03T00:00:00Z',
  first_discovered_at: '2026-01-02T00:00:00Z',
  edition_count: 1,
  providers: ['wnacg'],
  authors: [{ id: 2, name: 'YD' }],
  editions: [{
    work_id: 20,
    title: '[其他作者] 作品 [YD汉化]',
    description: null,
    cover_url: null,
    status: null,
    year: null,
    language: 'zh-hans',
    tags: [],
    variant_labels: [],
    latest_source_at: null,
    confidence: 1,
    match_method: 'new',
    is_manual: false,
    authors: [{ id: 2, name: 'YD' }],
    sources: [{ provider: 'wnacg', external_id: '1', source_url: 'https://example.test/1', source_updated_at: null }],
  }],
}

describe('WorkDetail correction and inline download actions', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('saves locked metadata and selects the preferred version', async () => {
    const onUpdateMetadata = vi.fn().mockResolvedValue(true)
    const detailed = { ...group, metadata_overrides: {}, metadata_sources: { title: { locked: false, edition_ids: [20], providers: ['wnacg'] } } }
    render(
      <WorkDetail
        group={detailed}
        allGroups={[detailed]}
        busy={false}
        enabledProviders={['wnacg']}
        activeAuthorId={2}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        onUpdateMetadata={onUpdateMetadata}
        onSplit={vi.fn()}
        onMerge={vi.fn()}
        onExcludeEdition={vi.fn()}
        onExcludeGroup={vi.fn()}
      />,
    )
    expect(screen.getByText(/标题：版本 20 · wnacg/)).toBeInTheDocument()
    expect(screen.getByText(/最早来源时间.*最近来源时间.*收录发现/)).toBeInTheDocument()
    expect(screen.getByText(/当前作者关联记录中的最早时间/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    fireEvent.click(screen.getByText('固定标题').closest('label')!.querySelector('input[type=checkbox]')!)
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: '我选的名称' } })
    fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    await waitFor(() => expect(onUpdateMetadata).toHaveBeenCalledWith(expect.objectContaining({ title: '我选的名称', year: null })))
    fireEvent.click(screen.getByRole('button', { name: '设为首选版本' }))
    await waitFor(() => expect(onUpdateMetadata).toHaveBeenCalledWith({ preferred_work_id: 20 }))
  })

  it('can exclude one edition or the whole group for the selected author', () => {
    const onExcludeEdition = vi.fn()
    const onExcludeGroup = vi.fn()
    render(
      <WorkDetail
        group={group}
        allGroups={[group]}
        busy={false}
        enabledProviders={['wnacg']}
        activeAuthorId={2}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        onUpdateMetadata={vi.fn().mockResolvedValue(true)}
        onSplit={vi.fn()}
        onMerge={vi.fn()}
        onExcludeEdition={onExcludeEdition}
        onExcludeGroup={onExcludeGroup}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '不是该作者的版本' }))
    fireEvent.click(screen.getByRole('button', { name: '整部都不是该作者作品' }))

    expect(onExcludeEdition).toHaveBeenCalledWith(2, 20)
    expect(onExcludeGroup).toHaveBeenCalledWith(2)
  })

  it('defers building large manual merge selectors until requested', () => {
    const manyGroups = Array.from({ length: 30 }, (_, index) => ({
      ...group,
      id: index + 100,
      title: `合并目标 ${index + 1}`,
    }))
    render(
      <WorkDetail
        group={group}
        allGroups={manyGroups}
        busy={false}
        enabledProviders={['wnacg']}
        activeAuthorId={2}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        onUpdateMetadata={vi.fn().mockResolvedValue(true)}
        onSplit={vi.fn()}
        onMerge={vi.fn()}
        onExcludeEdition={vi.fn()}
        onExcludeGroup={vi.fn()}
      />,
    )

    expect(screen.queryByRole('option', { name: '合并目标 30' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '选择目标作品…' }))
    expect(screen.getByRole('option', { name: '合并目标 30' })).toBeInTheDocument()
  })

  it('checks library and chapters inline before queueing a download without window.confirm', async () => {
    apiMocks.libraryCheck.mockResolvedValue({ status: 'not_found', message: '未在馆藏中发现此版本', matches: [] })
    apiMocks.chapters.mockResolvedValue([
      { external_id: 'ch-1', title: '第一话', number: '1', language: 'zh', published_at: null, source_url: '' },
      { external_id: 'ch-2', title: '第二话', number: '2', language: 'zh', published_at: null, source_url: '' },
    ])
    apiMocks.downloadChapter.mockResolvedValue({ id: 99 })
    const onDownloadQueued = vi.fn().mockResolvedValue(undefined)

    render(
      <WorkDetail
        group={group}
        allGroups={[group]}
        busy={false}
        enabledProviders={['wnacg']}
        activeAuthorId={2}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        onDownloadQueued={onDownloadQueued}
        onUpdateMetadata={vi.fn().mockResolvedValue(true)}
        onSplit={vi.fn()}
        onMerge={vi.fn()}
        onExcludeEdition={vi.fn()}
        onExcludeGroup={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '下载 CBZ ↓' }))
    expect(await screen.findByText('未在馆藏中发现此版本')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('选择章节'), { target: { value: 'ch-2' } })
    fireEvent.click(screen.getByRole('button', { name: '确认加入下载队列' }))

    await waitFor(() => expect(apiMocks.downloadChapter).toHaveBeenCalledWith(20, 'wnacg', 'ch-2', false))
    expect(await screen.findByText('✓ 已加入后台 CBZ 下载队列')).toBeInTheDocument()
    expect(onDownloadQueued).toHaveBeenCalled()
  })
})
