import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PageSelector } from './PageSelector'

describe('PageSelector', () => {
  afterEach(cleanup)

  it('opens a scrollable page list and selects a page', () => {
    const onChange = vi.fn()
    render(<PageSelector page={12} pageCount={48} totalItems={1130} onChange={onChange} />)

    const trigger = screen.getByRole('button', { name: '第 12 / 48 页' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(trigger)

    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('listbox', { name: '可选页码' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '12' })).toHaveAttribute('aria-current', 'page')
    fireEvent.click(screen.getByRole('option', { name: '35' }))

    expect(onChange).toHaveBeenCalledWith(35)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('supports direct page jumps and rejects out-of-range values', () => {
    const onChange = vi.fn()
    render(<PageSelector page={3} pageCount={10} totalItems={230} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '第 3 / 10 页' }))

    const input = screen.getByLabelText('跳转页码')
    fireEvent.change(input, { target: { value: '11' } })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: '前往' })).toBeDisabled()

    fireEvent.change(input, { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: '前往' }))
    expect(onChange).toHaveBeenCalledWith(8)
  })
})
