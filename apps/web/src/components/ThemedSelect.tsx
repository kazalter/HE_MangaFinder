import { CaretDown, Check } from '@phosphor-icons/react'
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'

type SelectValue = string | number

export function ThemedSelect<T extends SelectValue>({ label, value, options, onChange }: {
  label: string
  value: T
  options: readonly { value: T, label: string }[]
  onChange: (value: T) => void
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])
  const listId = useId()
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value))
  const selected = options[selectedIndex] ?? options[0]

  useEffect(() => {
    if (!open) return
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      triggerRef.current?.focus()
    }
    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    window.requestAnimationFrame(() => optionRefs.current[selectedIndex]?.focus())
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open, selectedIndex])

  function openFromKeyboard(event: KeyboardEvent<HTMLButtonElement>) {
    if (!['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) return
    event.preventDefault()
    setOpen(true)
  }

  function navigateOptions(event: KeyboardEvent<HTMLDivElement>) {
    const activeIndex = optionRefs.current.findIndex((option) => option === document.activeElement)
    let nextIndex = activeIndex < 0 ? selectedIndex : activeIndex
    if (event.key === 'ArrowDown') nextIndex = (nextIndex + 1) % options.length
    else if (event.key === 'ArrowUp') nextIndex = (nextIndex - 1 + options.length) % options.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = options.length - 1
    else return
    event.preventDefault()
    optionRefs.current[nextIndex]?.focus()
  }

  function choose(nextValue: T) {
    onChange(nextValue)
    setOpen(false)
    triggerRef.current?.focus()
  }

  return (
    <div className={`themed-select${open ? ' is-open' : ''}`} ref={rootRef}>
      <span className="themed-select-label">{label}</span>
      <button
        type="button"
        className="themed-select-trigger"
        ref={triggerRef}
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-haspopup="listbox"
        data-value={String(value)}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={openFromKeyboard}
      >
        <span>{selected?.label}</span><CaretDown size={15} weight="bold" />
      </button>
      <div className="themed-select-menu" id={listId} role="listbox" aria-label={`${label}选项`} aria-hidden={!open} inert={!open} onKeyDown={navigateOptions}>
        <div className="themed-select-menu-inner">
          {options.map((option, index) => (
            <button
              type="button"
              role="option"
              aria-selected={option.value === value}
              tabIndex={open ? 0 : -1}
              ref={(element) => { optionRefs.current[index] = element }}
              onClick={() => choose(option.value)}
              key={option.value}
            >
              <span>{option.label}</span>{option.value === value && <Check size={16} weight="bold" />}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
