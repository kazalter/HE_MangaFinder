import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ThemedSelect } from './ThemedSelect'

describe('ThemedSelect', () => {
  afterEach(cleanup)

  it('opens, selects an option, and closes with the new value', () => {
    const onChange = vi.fn()
    const { container } = render(
      <ThemedSelect label="排序" value="first" options={[
        { value: 'first', label: '作品时间' },
        { value: 'title', label: '标题' },
      ]} onChange={onChange} />,
    )

    fireEvent.click(screen.getByRole('combobox', { name: '排序' }))
    expect(container.querySelector('.themed-select')).toHaveClass('is-open')
    fireEvent.click(screen.getByRole('option', { name: '标题' }))
    expect(onChange).toHaveBeenCalledWith('title')
    expect(container.querySelector('.themed-select')).not.toHaveClass('is-open')
  })

  it('supports keyboard navigation and outside-click closing', async () => {
    const { container } = render(
      <ThemedSelect label="筛选" value="all" options={[
        { value: 'all', label: '全部作品' },
        { value: 'multi', label: '多版本' },
      ]} onChange={vi.fn()} />,
    )
    const trigger = screen.getByRole('combobox', { name: '筛选' })

    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByRole('option', { name: '全部作品' })).toHaveFocus())
    fireEvent.keyDown(screen.getByRole('option', { name: '全部作品' }), { key: 'ArrowDown' })
    expect(screen.getByRole('option', { name: '多版本' })).toHaveFocus()

    fireEvent.pointerDown(document.body)
    expect(container.querySelector('.themed-select')).not.toHaveClass('is-open')
  })
})
