import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react'
import { createPortal } from 'react-dom'
import type { WorkGroup } from '../types'
import { CoverImage } from './CoverImage'
import { statusMap, workAuthorLabel } from './WorkCard'

const previewOpenDelayMs = 240
const previewHandoffDelayMs = 90
const previewCloseDelayMs = 110
const scrollCooldownMs = 180
const popoverGapPx = 14
const viewportMarginPx = 16
const fallbackPopoverWidth = 436
const fallbackPopoverHeight = 272
const textColumnSelector = '.list-title, .list-state, .list-sources, .list-update'
const listDateFormatter = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' })

type PreviewCloser = () => void
type PopoverPos = { left: number; top: number }

let activePreviewCloser: PreviewCloser | null = null
let hoveredPreviewStarter: (() => void) | null = null
let previewSuppressedUntil = 0
let scrollResumeTimer: number | null = null
let globalPreviewListenersReady = false
let mountedRowCount = 0

function closeActivePreview() {
  const close = activePreviewCloser
  activePreviewCloser = null
  close?.()
}

const suppressDuringScroll = () => {
  previewSuppressedUntil = Date.now() + scrollCooldownMs
  closeActivePreview()
  if (scrollResumeTimer !== null) window.clearTimeout(scrollResumeTimer)
  scrollResumeTimer = window.setTimeout(() => {
    scrollResumeTimer = null
    previewSuppressedUntil = 0
    hoveredPreviewStarter?.()
  }, scrollCooldownMs)
}

const handleGlobalScroll = () => {
  if (!activePreviewCloser && !hoveredPreviewStarter) {
    previewSuppressedUntil = Date.now() + scrollCooldownMs
    return
  }
  suppressDuringScroll()
}

const handleGlobalWheel = () => {
  if (!activePreviewCloser && !hoveredPreviewStarter) {
    previewSuppressedUntil = Date.now() + scrollCooldownMs
    return
  }
  suppressDuringScroll()
}

const closeForViewportChange = () => {
  previewSuppressedUntil = Date.now() + scrollCooldownMs
  closeActivePreview()
}

function ensureGlobalPreviewListeners() {
  if (globalPreviewListenersReady || typeof window === 'undefined') return
  window.addEventListener('wheel', handleGlobalWheel, { passive: true })
  window.addEventListener('scroll', handleGlobalScroll, { capture: true, passive: true })
  window.addEventListener('resize', closeForViewportChange, { passive: true })
  globalPreviewListenersReady = true
}

function releaseGlobalPreviewListeners() {
  if (scrollResumeTimer !== null) {
    window.clearTimeout(scrollResumeTimer)
    scrollResumeTimer = null
  }
  previewSuppressedUntil = 0
  hoveredPreviewStarter = null
  activePreviewCloser = null
  if (!globalPreviewListenersReady || typeof window === 'undefined') return
  window.removeEventListener('wheel', handleGlobalWheel)
  window.removeEventListener('scroll', handleGlobalScroll, { capture: true })
  window.removeEventListener('resize', closeForViewportChange)
  globalPreviewListenersReady = false
}

function supportsHoverPreview() {
  return !window.matchMedia || window.matchMedia('(hover: hover) and (pointer: fine)').matches
}

function displayDate(value: string | null) {
  return value ? listDateFormatter.format(new Date(value)) : '来源时间未知'
}

function cleanWorkDescription(description: string | null) {
  if (!description) return ''
  return description.replace(/\[[^\]]+\]\([^)]+\)/g, '').trim()
}

function computePopoverPosition(
  triggerRect: DOMRect,
  popoverRect: { width: number; height: number },
  viewport: { width: number; height: number },
): PopoverPos {
  const maxWidth = Math.max(200, viewport.width - viewportMarginPx * 2)
  const width = Math.min(popoverRect.width > 0 ? popoverRect.width : fallbackPopoverWidth, maxWidth)
  const height = popoverRect.height > 0 ? popoverRect.height : fallbackPopoverHeight

  const preferredLeft = triggerRect.right + popoverGapPx
  const left = preferredLeft + width <= viewport.width - viewportMarginPx
    ? preferredLeft
    : Math.max(viewportMarginPx, viewport.width - width - viewportMarginPx)

  const idealTop = triggerRect.top + triggerRect.height / 2 - height / 2
  const maxTop = Math.max(viewportMarginPx, viewport.height - height - viewportMarginPx)
  const top = Math.max(viewportMarginPx, Math.min(idealTop, maxTop))

  return { left: Math.round(left), top: Math.round(top) }
}

export const WorkListRow = memo(function WorkListRow({
  work,
  onOpen,
}: {
  work: WorkGroup
  onOpen: (work: WorkGroup) => void
}) {
  const rowRef = useRef<HTMLButtonElement>(null)
  const triggerRef = useRef<HTMLSpanElement>(null)
  const popoverRef = useRef<HTMLElement>(null)
  const timerRef = useRef<number | null>(null)
  const [preview, setPreview] = useState(false)
  const [popoverPos, setPopoverPos] = useState<PopoverPos | null>(null)

  useEffect(() => {
    mountedRowCount += 1
    return () => {
      mountedRowCount = Math.max(0, mountedRowCount - 1)
      if (mountedRowCount === 0) releaseGlobalPreviewListeners()
    }
  }, [])

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const closePreview = useCallback(() => {
    clearTimer()
    setPreview(false)
    setPopoverPos(null)
  }, [clearTimer])

  const releasePreview = useCallback(() => {
    closePreview()
    if (activePreviewCloser === closePreview) activePreviewCloser = null
  }, [closePreview])

  const showPreview = useCallback(() => {
    if (!work.cover_url) return
    if (triggerRef.current && typeof window !== 'undefined') {
      const next = computePopoverPosition(
        triggerRef.current.getBoundingClientRect(),
        { width: 0, height: 0 },
        { width: window.innerWidth, height: window.innerHeight },
      )
      setPopoverPos(next)
    }
    setPreview(true)
  }, [work.cover_url])

  const schedulePreview = useCallback(() => {
    ensureGlobalPreviewListeners()
    if (!work.cover_url || !supportsHoverPreview() || Date.now() < previewSuppressedUntil) return
    clearTimer()
    // Cold dwell stays conservative (240ms) to avoid casual sweeps; switching between covers is quicker.
    const delay = activePreviewCloser && activePreviewCloser !== closePreview
      ? previewHandoffDelayMs
      : previewOpenDelayMs
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      if (hoveredPreviewStarter !== schedulePreview) return
      if (activePreviewCloser !== closePreview) activePreviewCloser?.()
      activePreviewCloser = closePreview
      showPreview()
    }, delay)
  }, [clearTimer, closePreview, showPreview, work.cover_url])

  const handleCoverPointerEnter = useCallback((event: PointerEvent<HTMLSpanElement>) => {
    if (event.pointerType === 'touch') return
    hoveredPreviewStarter = schedulePreview
    schedulePreview()
  }, [schedulePreview])

  const handleCoverPointerLeave = useCallback((event: PointerEvent<HTMLSpanElement>) => {
    if (event.pointerType === 'touch') return
    if (hoveredPreviewStarter === schedulePreview) hoveredPreviewStarter = null
    clearTimer()
    timerRef.current = window.setTimeout(releasePreview, previewCloseDelayMs)
  }, [clearTimer, releasePreview, schedulePreview])

  const dismissWhenEnteringTextColumn = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (event.pointerType === 'touch') return
    const target = event.target instanceof Element ? event.target : null
    if (!target || triggerRef.current?.contains(target)) return
    if (!target.closest(textColumnSelector)) return
    hoveredPreviewStarter = null
    clearTimer()
    closeActivePreview()
  }, [clearTimer])

  const handleFocus = useCallback(() => {
    if (!work.cover_url) return
    window.requestAnimationFrame(() => {
      if (!rowRef.current?.matches(':focus-visible')) return
      ensureGlobalPreviewListeners()
      if (activePreviewCloser !== closePreview) activePreviewCloser?.()
      activePreviewCloser = closePreview
      showPreview()
    })
  }, [closePreview, showPreview, work.cover_url])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Escape' && preview) {
      event.stopPropagation()
      releasePreview()
    }
  }, [preview, releasePreview])

  useLayoutEffect(() => {
    if (!preview || !triggerRef.current || !popoverRef.current) return
    const next = computePopoverPosition(
      triggerRef.current.getBoundingClientRect(),
      popoverRef.current.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight },
    )
    setPopoverPos((prev) => (prev && prev.left === next.left && prev.top === next.top ? prev : next))
  }, [preview, work.id])

  useEffect(() => () => {
    if (hoveredPreviewStarter === schedulePreview) hoveredPreviewStarter = null
    releasePreview()
  }, [releasePreview, schedulePreview])

  const descriptionText = cleanWorkDescription(work.description)
  const popoverStyle: CSSProperties | undefined = popoverPos
    ? { left: `${popoverPos.left}px`, top: `${popoverPos.top}px` }
    : undefined

  return (
    <>
      <button
        className={`work-list-row${preview ? ' preview-active' : ''}`}
        onClick={() => {
          if (hoveredPreviewStarter === schedulePreview) hoveredPreviewStarter = null
          releasePreview()
          onOpen(work)
        }}
        onPointerEnter={dismissWhenEnteringTextColumn}
        onPointerMove={dismissWhenEnteringTextColumn}
        onFocus={handleFocus}
        onBlur={releasePreview}
        onKeyDown={handleKeyDown}
        ref={rowRef}
      >
        <span
          className="list-cover"
          data-testid="list-cover-trigger"
          ref={triggerRef}
          onPointerEnter={handleCoverPointerEnter}
          onPointerLeave={handleCoverPointerLeave}
        >
          <CoverImage
            src={work.cover_url}
            alt={`${work.title} 封面`}
            loading="lazy"
            fallback={<span className="list-cover-empty">无封面</span>}
          />
        </span>
        <span className="list-title">
          <small>{workAuthorLabel(work)}</small>
          <strong>{work.title}</strong>
          <span>{work.tags.slice(0, 3).join(' · ') || '暂无标签'}</span>
        </span>
        <span className="list-state">
          <strong>{work.status ? statusMap[work.status] ?? work.status : '状态未知'}</strong>
          <small>{[work.year, work.language?.toUpperCase()].filter(Boolean).join(' · ') || '年份未知'}</small>
        </span>
        <span className="list-sources">
          {work.providers.map((provider) => (
            <span className={`source-badge source-${provider}`} key={provider}>{provider}</span>
          ))}
        </span>
        <span className="list-update">
          <strong>{work.edition_count} 个版本</strong>
          <small>{displayDate(work.first_source_at)}</small>
        </span>
      </button>
      {preview && work.cover_url && typeof document !== 'undefined' && createPortal(
        <aside
          ref={popoverRef}
          className="work-list-popover"
          data-testid="work-list-preview"
          aria-hidden="true"
          style={popoverStyle}
        >
          <div className="work-list-popover-cover">
            <CoverImage
              src={work.cover_url}
              alt={`${work.title} 封面预览`}
              loading="eager"
              fallback={<span className="list-cover-empty">无封面</span>}
            />
            {work.status && <span className="status-pill">{statusMap[work.status] ?? work.status}</span>}
          </div>
          <div className="work-list-popover-body">
            <p className="work-list-popover-author">{workAuthorLabel(work)}</p>
            <h4>{work.title}</h4>
            <p className="work-list-popover-meta">
              {[work.year, work.language?.toUpperCase(), `${work.edition_count} 个版本`, displayDate(work.first_source_at)]
                .filter(Boolean)
                .join(' · ')}
            </p>
            {descriptionText && <p className="work-list-popover-desc">{descriptionText}</p>}
            {work.tags.length > 0 && (
              <div className="work-list-popover-tags">
                {work.tags.slice(0, 6).map((tag) => <span key={tag}>{tag}</span>)}
                {work.tags.length > 6 && <span className="is-more">+{work.tags.length - 6}</span>}
              </div>
            )}
            {work.providers.length > 0 && (
              <div className="work-list-popover-sources">
                {work.providers.map((provider) => (
                  <span className={`source-badge source-${provider}`} key={provider}>{provider}</span>
                ))}
              </div>
            )}
          </div>
        </aside>,
        document.body,
      )}
    </>
  )
})
