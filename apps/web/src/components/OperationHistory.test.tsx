import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { OperationHistory } from './OperationHistory'

const { list, undo } = vi.hoisted(() => ({ list: vi.fn(), undo: vi.fn() }))
vi.mock('../lib/api', () => ({ api: { catalogOperations: list, undoCatalogOperation: undo } }))

describe('OperationHistory', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows before and after and marks an undone operation', async () => {
    const row = { id: 1, kind: 'merge', group_ids: [1, 2], before_group_ids: [1, 2], after_group_ids: [1], summary: '合并作品', created_at: '2026-11-01T10:00:00Z', undone_at: null }
    list.mockResolvedValue([row])
    undo.mockResolvedValue({ ...row, undone_at: '2026-11-02T10:00:00Z' })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const changed = vi.fn().mockResolvedValue(undefined)
    render(<OperationHistory onClose={vi.fn()} onChanged={changed} />)
    expect(await screen.findByText('合并作品')).toBeInTheDocument()
    expect(screen.getByText(/整理前 2 部 → 整理后 1 部/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(undo).toHaveBeenCalledWith(1))
    expect(await screen.findByText(/已撤销/)).toBeInTheDocument()
    expect(changed).toHaveBeenCalledOnce()
  })
})
