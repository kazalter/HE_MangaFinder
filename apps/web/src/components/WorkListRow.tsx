import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from 'react'
import type { WorkGroup } from '../types'
import { CoverImage } from './CoverImage'
import { statusMap, workAuthorLabel } from './WorkCard'

const previewOpenDelayMs = 70
const previewCloseDelayMs = 140
const scrollCooldownMs = 180

type PreviewCloser = (restoreAnchor?: boolean) => void

let activePreviewCloser: PreviewCloser | null = null
let hoveredPreviewStarter: (() => void) | null = null
let previewSuppressedUntil = 0
let internalScrollUntil = 0
let scrollResumeTimer: number | null = null
let globalPreviewListenersReady = false

function closeActivePreview(restoreAnchor = false) {
  const close = activePreviewCloser
  activePreviewCloser = null
  close?.(restoreAnchor)
}

function adjustViewport(delta: number) {
  if (Math.abs(delta) < 0.5) return 0
  internalScrollUntil = Date.now() + 120
  const before = window.scrollY
  window.scrollBy({ top: delta, left: 0, behavior: 'auto' })
  return window.scrollY - before
}

function ensureGlobalPreviewListeners() {
  if (globalPreviewListenersReady || typeof window === 'undefined') return
  const suppressDuringScroll = () => {
    previewSuppressedUntil = Date.now() + scrollCooldownMs
    closeActivePreview(false)
    if (scrollResumeTimer !== null) window.clearTimeout(scrollResumeTimer)
    scrollResumeTimer = window.setTimeout(() => {
      scrollResumeTimer = null
      previewSuppressedUntil = 0
      hoveredPreviewStarter?.()
    }, scrollCooldownMs)
  }
  const handleScroll = () => {
    if (Date.now() < internalScrollUntil) return
    suppressDuringScroll()
  }
  const handleWheel = () => {
    internalScrollUntil = 0
    suppressDuringScroll()
  }
  const closeForViewportChange = () => {
    previewSuppressedUntil = Date.now() + scrollCooldownMs
    closeActivePreview(false)
  }
  window.addEventListener('wheel', handleWheel, { passive: true })
  window.addEventListener('scroll', handleScroll, { capture: true, passive: true })
  window.addEventListener('resize', closeForViewportChange, { passive: true })
  globalPreviewListenersReady = true
}

function supportsHoverPreview() {
  return !window.matchMedia || window.matchMedia('(hover: hover) and (pointer: fine)').matches
}

function displayDate(value: string | null) {
  return value
    ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }).format(new Date(value))
    : '来源时间未知'
}

export function WorkListRow({
  work,
  onOpen,
}: {
  work: WorkGroup
  onOpen: (work: WorkGroup) => void
}) {
  const rowRef = useRef<HTMLButtonElement>(null)
  const timerRef = useRef<number | null>(null)
  const pointerYRef = useRef<number | null>(null)
  const anchorScrollRef = useRef(0)
  const [preview, setPreview] = useState(false)

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const closePreview = useCallback((restoreAnchor = true) => {
    clearTimer()
    if (restoreAnchor && anchorScrollRef.current) {
      adjustViewport(-anchorScrollRef.current)
    }
    anchorScrollRef.current = 0
    setPreview(false)
  }, [clearTimer])

  const releasePreview = useCallback(() => {
    closePreview(true)
    if (activePreviewCloser === closePreview) activePreviewCloser = null
  }, [closePreview])

  const showPreview = useCallback(() => {
    if (work.cover_url) setPreview(true)
  }, [work.cover_url])

  const schedulePreview = useCallback(() => {
    ensureGlobalPreviewListeners()
    if (!work.cover_url || !supportsHoverPreview() || Date.now() < previewSuppressedUntil) return
    clearTimer()
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      if (hoveredPreviewStarter !== schedulePreview) return
      if (activePreviewCloser !== closePreview) activePreviewCloser?.(true)
      activePreviewCloser = closePreview
      showPreview()
    }, previewOpenDelayMs)
  }, [clearTimer, closePreview, showPreview, work.cover_url])

  const handlePointerEnter = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    pointerYRef.current = event.clientY
    hoveredPreviewStarter = schedulePreview
    schedulePreview()
  }, [schedulePreview])

  const scheduleClose = useCallback(() => {
    if (hoveredPreviewStarter === schedulePreview) hoveredPreviewStarter = null
    clearTimer()
    timerRef.current = window.setTimeout(releasePreview, previewCloseDelayMs)
  }, [clearTimer, releasePreview, schedulePreview])

  const handleFocus = useCallback(() => {
    if (!work.cover_url) return
    window.requestAnimationFrame(() => {
      if (!rowRef.current?.matches(':focus-visible')) return
      ensureGlobalPreviewListeners()
      const rect = rowRef.current.getBoundingClientRect()
      pointerYRef.current = rect.top + rect.height / 2
      if (activePreviewCloser !== closePreview) activePreviewCloser?.(true)
      activePreviewCloser = closePreview
      showPreview()
    })
  }, [closePreview, showPreview, work.cover_url])

  useLayoutEffect(() => {
    const row = rowRef.current
    const targetY = pointerYRef.current
    if (!preview || !row || targetY === null) return

    const alignRowCenter = () => {
      const rect = row.getBoundingClientRect()
      if (rect.height <= 0) return
      anchorScrollRef.current += adjustViewport(rect.top + rect.height / 2 - targetY)
    }
    alignRowCenter()
    if (typeof ResizeObserver === 'undefined') {
      const frame = window.requestAnimationFrame(alignRowCenter)
      return () => window.cancelAnimationFrame(frame)
    }
    const observer = new ResizeObserver(alignRowCenter)
    observer.observe(row)
    return () => observer.disconnect()
  }, [preview])

  useEffect(() => () => {
    if (hoveredPreviewStarter === schedulePreview) hoveredPreviewStarter = null
    releasePreview()
  }, [releasePreview, schedulePreview])

  return (
    <button
      className={`work-list-row${preview ? ' preview-active' : ''}`}
      onClick={() => {
        releasePreview()
        onOpen(work)
      }}
      onPointerEnter={handlePointerEnter}
      onPointerLeave={scheduleClose}
      onFocus={handleFocus}
      onBlur={releasePreview}
      ref={rowRef}
    >
      <span className="list-cover" data-testid="list-cover-trigger">
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
  )
}
