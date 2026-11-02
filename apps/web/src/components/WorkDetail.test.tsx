import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkDetail } from './WorkDetail'

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
  latest_source_at: null,
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

describe('WorkDetail correction actions', () => {
  afterEach(cleanup)
  it('saves locked metadata and selects the preferred version', async () => {
    const onUpdateMetadata = vi.fn().mockResolvedValue(true)
    const detailed = { ...group, metadata_overrides: {}, metadata_sources: { title: { locked: false, edition_ids: [20], providers: ['wnacg'] } } }
    render(<WorkDetail
      group={detailed} allGroups={[detailed]} busy={false} enabledProviders={['wnacg']} activeAuthorId={2}
      onClose={vi.fn()} onDownload={vi.fn()} onUpdateMetadata={onUpdateMetadata}
      onSplit={vi.fn()} onMerge={vi.fn()} onExcludeEdition={vi.fn()} onExcludeGroup={vi.fn()}
    />)
    expect(screen.getByText(/标题：版本 20 · wnacg/)).toBeInTheDocument()
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
    render(<WorkDetail
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
    />)

    fireEvent.click(screen.getByRole('button', { name: '不是该作者的版本' }))
    fireEvent.click(screen.getByRole('button', { name: '整部都不是该作者作品' }))

    expect(onExcludeEdition).toHaveBeenCalledWith(2, 20)
    expect(onExcludeGroup).toHaveBeenCalledWith(2)
  })
})
