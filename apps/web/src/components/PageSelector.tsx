import { CaretDown, Check } from '@phosphor-icons/react'
import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'

interface Props {
  page: number
  pageCount: number
  totalItems: number
  onChange: (page: number) => void
}

export function PageSelector({ page, pageCount, totalItems, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [pageInput, setPageInput] = useState(String(page))
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])
  const panelId = useId()
  const pages = Array.from({ length: pageCount }, (_, index) => index + 1)
  const inputPage = Number(pageInput)
  const inputValid = /^\d+$/.test(pageInput)
    && Number.isSafeInteger(inputPage)
    && inputPage >= 1
    && inputPage <= pageCount

  useEffect(() => { setPageInput(String(page)) }, [page])

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
    window.requestAnimationFrame(() => {
      const selected = optionRefs.current[page - 1]
      selected?.scrollIntoView?.({ block: 'center' })
      selected?.focus()
    })
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open, page])

  function choose(nextPage: number) {
    onChange(nextPage)
    setOpen(false)
    triggerRef.current?.focus()
  }

  function submitJump(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!inputValid) {
      setPageInput(String(page))
      return
    }
    choose(inputPage)
  }

  function navigatePages(event: KeyboardEvent<HTMLDivElement>) {
    const currentIndex = optionRefs.current.findIndex((option) => option === document.activeElement)
    if (currentIndex < 0) return
    let nextIndex = currentIndex
    if (event.key === 'ArrowRight') nextIndex += 1
    else if (event.key === 'ArrowLeft') nextIndex -= 1
    else if (event.key === 'ArrowDown') nextIndex += 5
    else if (event.key === 'ArrowUp') nextIndex -= 5
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = pageCount - 1
    else return
    event.preventDefault()
    optionRefs.current[Math.max(0, Math.min(pageCount - 1, nextIndex))]?.focus()
  }

  return <div className={`page-selector${open ? ' is-open' : ''}`} ref={rootRef}>
    <button
      type="button"
      className="page-selector-trigger"
      ref={triggerRef}
      aria-expanded={open}
      aria-controls={panelId}
      aria-haspopup="dialog"
      onClick={() => setOpen((current) => !current)}
    >
      <span>第 <b>{page}</b> / {pageCount} 页</span><CaretDown size={14} weight="bold" />
    </button>
    <section className="page-selector-panel" id={panelId} role="dialog" aria-label="选择页码" aria-hidden={!open} inert={!open}>
      <header><div><strong>选择页码</strong><small>共 {totalItems} 部作品</small></div><span>{pageCount} 页</span></header>
      <form className="page-selector-jump" onSubmit={submitJump}>
        <label htmlFor={`${panelId}-input`}>跳至</label>
        <input
          id={`${panelId}-input`}
          value={pageInput}
          onChange={(event) => { if (/^\d*$/.test(event.target.value)) setPageInput(event.target.value) }}
          onFocus={(event) => event.currentTarget.select()}
          inputMode="numeric"
          autoComplete="off"
          aria-label="跳转页码"
          aria-invalid={pageInput !== '' && !inputValid}
        />
        <span>页</span><button disabled={!inputValid}>前往</button>
      </form>
      <div className="page-selector-scroll" role="listbox" aria-label="可选页码" onKeyDown={navigatePages}>
        <div className="page-selector-grid">
          {pages.map((item) => <button
            type="button"
            role="option"
            aria-selected={item === page}
            aria-current={item === page ? 'page' : undefined}
            tabIndex={open && item === page ? 0 : -1}
            ref={(element) => { optionRefs.current[item - 1] = element }}
            onClick={() => choose(item)}
            key={item}
          ><span>{item}</span>{item === page && <Check size={13} weight="bold" />}</button>)}
        </div>
      </div>
    </section>
  </div>
}
