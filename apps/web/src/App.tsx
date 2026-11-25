import { lazy, Suspense, useCallback, useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { AuthorSidebar } from './components/AuthorSidebar'
import { Dashboard } from './components/Dashboard'
import { PageSelector } from './components/PageSelector'
import { ThemedSelect } from './components/ThemedSelect'
import { WorkCard } from './components/WorkCard'
import { WorkCoverTile } from './components/WorkCoverTile'
import { WorkListRow } from './components/WorkListRow'
import { api } from './lib/api'
import type { AgentStatus, Author, CatalogEvent, DashboardData, Edition, Job, MergeSuggestion, ReleaseSignal, SeriesCandidate, SocialStatus, Source, WorkGroup, WorkGroupDetail, WorkMetadataPatch, WorkSource } from './types'

const loadWorkDetail = () => import('./components/WorkDetail').then((m) => ({ default: m.WorkDetail }))
const WorkDetail = lazy(loadWorkDetail)
const AuthorDirectory = lazy(() => import('./components/AuthorDirectory').then((m) => ({ default: m.AuthorDirectory })))
const TodoWorkbench = lazy(() => import('./components/TodoWorkbench').then((m) => ({ default: m.TodoWorkbench })))
const MergeReview = lazy(() => import('./components/MergeReview').then((m) => ({ default: m.MergeReview })))
const OperationHistory = lazy(() => import('./components/OperationHistory').then((m) => ({ default: m.OperationHistory })))
const ImageSearch = lazy(() => import('./components/ImageSearch').then((m) => ({ default: m.ImageSearch })))
const AssistantChat = lazy(() => import('./components/AssistantChat').then((m) => ({ default: m.AssistantChat })))
const SocialRadar = lazy(() => import('./components/SocialRadar').then((m) => ({ default: m.SocialRadar })))
const SystemSettings = lazy(() => import('./components/SystemSettings').then((m) => ({ default: m.SystemSettings })))

type ViewMode = 'cards' | 'covers' | 'list'
type WorkFilter = 'all' | 'ongoing' | 'completed' | 'multi' | 'review'
type WorkSort = 'first' | 'updated' | 'title' | 'year' | 'editions'
type GroupMode = 'none' | 'author'
export type LibraryMode = 'overview' | 'works' | 'authors' | 'todo'

const JOB_LABELS: Record<string, string> = {
  discover_author: '作品发现',
  download_chapter: '章节下载',
  agent_review_suggestions: '智能聚合审核',
  social_sync_account: 'X 动态同步',
  deliver_notifications: '消息通知',
  build_daily_digest: '每日动态摘要',
  refresh_cover_fingerprints: '封面索引更新',
}

function savedChoice<T extends string>(key: string, choices: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key) as T | null
    return value && choices.includes(value) ? value : fallback
  } catch {
    return fallback
  }
}

function saveChoice(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Browsing still works when storage is disabled; only preference persistence is lost.
  }
}

const selectedAuthorKey = 'mangafinder:selected-author'
const standardPageSize = 24

export function calculateCoverColumns(width: number): number {
  const idealCoverWidth = width < 520 ? 130 : width < 900 ? 145 : width < 1300 ? 165 : 185
  const gap = width <= 600 ? 13 : 17
  return Math.max(2, Math.min(12, Math.floor((width + gap) / (idealCoverWidth + gap))))
}
const workFilterOptions: readonly { value: WorkFilter, label: string }[] = [
  { value: 'all', label: '全部作品' }, { value: 'ongoing', label: '连载中' },
  { value: 'completed', label: '已完结' }, { value: 'multi', label: '多版本' },
  { value: 'review', label: '待整理' },
]
const workSortOptions: readonly { value: WorkSort, label: string }[] = [
  { value: 'first', label: '最早来源时间' }, { value: 'updated', label: '最近有新版本' },
  { value: 'title', label: '标题' }, { value: 'year', label: '年份' },
  { value: 'editions', label: '版本数' },
]
const groupOptions: readonly { value: GroupMode, label: string }[] = [
  { value: 'none', label: '不分组' }, { value: 'author', label: '按作者' },
]
function savedAuthorId(): number | null {
  try {
    const urlValue = new URLSearchParams(window.location.search).get('author')
    const value = urlValue ?? window.localStorage.getItem(selectedAuthorKey)
    if (!value || !/^\d+$/.test(value)) return null
    const id = Number(value)
    return Number.isSafeInteger(id) && id > 0 ? id : null
  } catch {
    return null
  }
}

function saveAuthorId(id: number | null) {
  try {
    if (id === null) window.localStorage.removeItem(selectedAuthorKey)
    else window.localStorage.setItem(selectedAuthorKey, String(id))
  } catch {
    // Author selection remains usable for this page when storage is disabled.
  }
}

const jobTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'medium',
})
const workTitleCollator = new Intl.Collator('zh-CN')

function toTimestamp(value: string | null | undefined): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

function jobsEqual(left: Job[], right: Job[]): boolean {
  if (left.length !== right.length) return false
  for (let i = 0; i < left.length; i += 1) {
    const a = left[i]
    const b = right[i]
    if (
      a.id !== b.id
      || a.status !== b.status
      || a.attempts !== b.attempts
      || a.error !== b.error
      || a.started_at !== b.started_at
      || a.finished_at !== b.finished_at
      || a.payload.output_path !== b.payload.output_path
    ) {
      return false
    }
  }
  return true
}

function formatJobTime(value: string | null): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return jobTimeFormatter.format(date)
}

function friendlyJobError(message: string | null): string {
  if (!message) return '任务未返回具体原因'
  if (message.includes('All connection attempts failed')) {
    return 'X 采集器在服务启动时尚未就绪，或当时容器网络暂时不可达。'
  }
  return message
}

function jobSubject(job: Job): string {
  if (job.kind === 'social_sync_account') return `${job.kind}:${String(job.payload.account_id ?? '')}`
  if (job.kind === 'discover_author') return `${job.kind}:${String(job.payload.author_id ?? '')}`
  if (job.kind === 'download_chapter') {
    return [job.kind, job.payload.work_id, job.payload.provider, job.payload.chapter_id].map(String).join(':')
  }
  return job.kind
}

export default function App() {
  const [authors, setAuthors] = useState<Author[]>([])
  const [works, setWorks] = useState<WorkGroup[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([])
  const [seriesCandidates, setSeriesCandidates] = useState<SeriesCandidate[]>([])
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null)
  const [dashboard, setDashboard] = useState<DashboardData | null>(null)
  const [discoveryEvents, setDiscoveryEvents] = useState<CatalogEvent[]>([])
  const [openedWork, setOpenedWork] = useState<WorkGroupDetail | null>(null)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [imageSearchOpen, setImageSearchOpen] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  const [radarOpen, setRadarOpen] = useState(() => new URLSearchParams(window.location.search).has('radar'))
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [focusSignalId] = useState<number | null>(() => {
    const value = new URLSearchParams(window.location.search).get('radar')
    return value && /^\d+$/.test(value) ? Number(value) : null
  })
  const [socialStatus, setSocialStatus] = useState<SocialStatus | null>(null)
  const [socialSignals, setSocialSignals] = useState<ReleaseSignal[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(savedAuthorId)
  const [previewAuthorId, setPreviewAuthorId] = useState<number | null>(savedAuthorId)
  const [libraryMode, setLibraryMode] = useState<LibraryMode>(() => {
    const requested = new URLSearchParams(window.location.search).get('view')
    if (requested === 'todo') return 'todo'
    if (requested === 'authors') return 'authors'
    if (requested === 'overview') return 'overview'
    return savedChoice('mangafinder:library-mode', ['overview', 'works', 'authors', 'todo'], 'overview')
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [viewMode, setViewMode] = useState<ViewMode>(() => savedChoice('mangafinder:view', ['cards', 'covers', 'list'], 'cards'))
  const [workFilter, setWorkFilter] = useState<WorkFilter>('all')
  const [workSort, setWorkSort] = useState<WorkSort>(() => savedChoice('mangafinder:sort-v2', ['first', 'updated', 'title', 'year', 'editions'], 'first'))
  const [groupMode, setGroupMode] = useState<GroupMode>(() => savedChoice('mangafinder:group', ['none', 'author'], 'none'))
  const [page, setPage] = useState(1)
  const [coverColumns, setCoverColumns] = useState(6)
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    try { return window.localStorage.getItem('mangafinder:library-sidebar-expanded') !== 'false' }
    catch { return true }
  })
  const catalogTopRef = useRef<HTMLElement>(null)
  const [dismissedJobThrough, setDismissedJobThrough] = useState(() => {
    try { return Number(window.sessionStorage.getItem('mangafinder:dismissed-job-through') ?? 0) || 0 }
    catch { return 0 }
  })
  const hasModal = !settingsOpen && Boolean(openedWork || reviewOpen || radarOpen || imageSearchOpen || historyOpen || assistantOpen)

  const loadGeneration = useRef(0)
  const load = useCallback(async () => {
    const generation = ++loadGeneration.current
    const failed: string[] = []
    const activeAuthorId = libraryMode === 'works' ? selectedId : null
    const update = async <T,>(label: string, request: Promise<T>, apply: (value: T) => void) => {
      try {
        const value = await request
        if (loadGeneration.current === generation) apply(value)
      } catch {
        failed.push(label)
      }
    }
    const tasks = [
      update('作者', api.authors(), (nextAuthors) => {
        setAuthors(nextAuthors)
        if (selectedId !== null && !nextAuthors.some((author) => author.id === selectedId)) setSelectedId(null)
        setPreviewAuthorId((current) => (
          current !== null && nextAuthors.some((author) => author.id === current)
            ? current : nextAuthors[0]?.id ?? null
        ))
      }),
      update('作品', api.workGroups(activeAuthorId ?? undefined), setWorks),
      update('任务', api.jobs(), setJobs),
      update('来源', api.sources(), setSources),
      update('聚合候选', api.mergeSuggestions(), setSuggestions),
      update('来源状态', api.socialStatus(), setSocialStatus),
    ]
    if (libraryMode === 'works') {
      tasks.push(update('系列候选', api.seriesCandidates(activeAuthorId ?? undefined), setSeriesCandidates))
    }
    if (libraryMode === 'overview') {
      tasks.push(update('资料库总览', api.dashboard(), setDashboard))
      tasks.push(update('新发现', api.discoveryInbox(), setDiscoveryEvents))
    }
    if (reviewOpen) tasks.push(update('聚合审核状态', api.agentStatus(), setAgentStatus))
    if (radarOpen) tasks.push(update('动态雷达', api.socialRadar(activeAuthorId ?? undefined), setSocialSignals))
    await Promise.all(tasks)
    if (loadGeneration.current === generation) {
      setError(failed.length ? `${failed.join('、')}加载失败，请重试` : null)
    }
  }, [libraryMode, selectedId, reviewOpen, radarOpen])

  const worksScope = libraryMode === 'works' && selectedId !== null ? String(selectedId) : 'all'
  const previousWorksScope = useRef(worksScope)
  useEffect(() => {
    if (previousWorksScope.current !== worksScope) setWorks([])
    previousWorksScope.current = worksScope
  }, [worksScope])
  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const openLogin = () => { setAssistantOpen(false); setSettingsOpen(true) }
    window.addEventListener('mangafinder:login-required', openLogin)
    return () => window.removeEventListener('mangafinder:login-required', openLogin)
  }, [])
  useEffect(() => {
    document.body.classList.toggle('modal-open', hasModal)
    return () => document.body.classList.remove('modal-open')
  }, [hasModal])
  useEffect(() => { saveChoice('mangafinder:view', viewMode) }, [viewMode])
  useEffect(() => { saveChoice('mangafinder:sort-v2', workSort) }, [workSort])
  useEffect(() => { saveChoice('mangafinder:group', groupMode) }, [groupMode])
  useEffect(() => { saveChoice('mangafinder:library-mode', libraryMode) }, [libraryMode])
  useEffect(() => { saveAuthorId(selectedId) }, [selectedId])
  useLayoutEffect(() => {
    if (viewMode !== 'covers' || libraryMode !== 'works' || settingsOpen || !works.length) return
    const element = catalogTopRef.current
    if (!element) return
    const updateWidth = (width: number) => {
      if (width <= 0) return
      const columns = calculateCoverColumns(width)
      setCoverColumns((current) => current === columns ? current : columns)
    }
    updateWidth(element.getBoundingClientRect().width)
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver((entries) => {
        const width = entries[0]?.contentRect.width ?? element.getBoundingClientRect().width
        updateWidth(width)
      })
      observer.observe(element)
      return () => observer.disconnect()
    }
    const measure = () => updateWidth(element.getBoundingClientRect().width)
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [libraryMode, settingsOpen, viewMode, works.length])
  useEffect(() => {
    const url = new URL(window.location.href)
    if (libraryMode === 'authors') url.searchParams.set('view', 'authors')
    else if (libraryMode === 'overview') url.searchParams.set('view', 'overview')
    else url.searchParams.delete('view')
    if (libraryMode === 'works' && selectedId !== null) url.searchParams.set('author', String(selectedId))
    else url.searchParams.delete('author')
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
  }, [libraryMode, selectedId])

  const activeJobs = useMemo(
    () => jobs.filter((job) => job.status === 'pending' || job.status === 'running'),
    [jobs],
  )
  const activeJobIds = useMemo(() => activeJobs.map((job) => job.id), [activeJobs])
  const activeJobIdsKey = activeJobIds.join(',')
  const activeJobIdsRef = useRef(activeJobIds)
  activeJobIdsRef.current = activeJobIds

  useEffect(() => {
    if (!activeJobIdsKey) return
    let cancelled = false
    let polling = false
    const timer = window.setInterval(() => {
      if (polling) return
      polling = true
      void api.jobs().then((nextJobs) => {
        if (cancelled) return
        setJobs((current) => jobsEqual(current, nextJobs) ? current : nextJobs)
        const nextActive = new Set(nextJobs.filter((job) => job.status === 'pending' || job.status === 'running').map((job) => job.id))
        if (activeJobIdsRef.current.some((id) => !nextActive.has(id))) void load()
      }).catch(() => {
        if (!cancelled) setError('任务状态加载失败，请重试')
      }).finally(() => { polling = false })
    }, 2000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [activeJobIdsKey, load])

  const selectedAuthor = useMemo(
    () => (libraryMode === 'works' ? authors.find((author) => author.id === selectedId) : undefined),
    [authors, libraryMode, selectedId],
  )
  const indexedWorks = useMemo(() => works.map((work) => ({
    work,
    firstTime: toTimestamp(work.first_source_at),
    latestTime: toTimestamp(work.latest_source_at),
    searchText: [
      work.title,
      ...work.authors.map((author) => author.name),
      ...work.tags,
      ...work.providers,
    ].join('\0').toLocaleLowerCase(),
  })), [works])
  const deferredSearch = useDeferredValue(search)
  const visibleWorks = useMemo(() => {
    const term = deferredSearch.trim().toLocaleLowerCase()
    const reviewIds = workFilter === 'review'
      ? new Set(suggestions.flatMap((item) => [item.source_group_id, item.target_group_id]))
      : null
    const filtered = indexedWorks.filter((entry) => {
      if (term && !entry.searchText.includes(term)) return false
      const { work } = entry
      if (workFilter === 'ongoing') return work.status === 'ongoing'
      if (workFilter === 'completed') return work.status === 'completed'
      if (workFilter === 'multi') return work.edition_count > 1
      if (workFilter === 'review') return reviewIds?.has(work.id) ?? false
      return true
    })
    filtered.sort((left, right) => {
      if (workSort === 'title') return workTitleCollator.compare(left.work.title, right.work.title)
      if (workSort === 'year') return (right.work.year ?? 0) - (left.work.year ?? 0) || workTitleCollator.compare(left.work.title, right.work.title)
      if (workSort === 'editions') return right.work.edition_count - left.work.edition_count || workTitleCollator.compare(left.work.title, right.work.title)
      if (workSort === 'first') return right.firstTime - left.firstTime
      return right.latestTime - left.latestTime
    })
    return filtered.map((entry) => entry.work)
  }, [deferredSearch, indexedWorks, suggestions, workFilter, workSort])
  const pageSize = viewMode === 'covers' ? coverColumns * 4 : standardPageSize
  const pageCount = Math.max(1, Math.ceil(visibleWorks.length / pageSize))
  const paginatedWorks = useMemo(
    () => visibleWorks.slice((page - 1) * pageSize, page * pageSize),
    [page, pageSize, visibleWorks],
  )
  const previousPageSizeRef = useRef(pageSize)
  useEffect(() => {
    const previousPageSize = previousPageSizeRef.current
    if (previousPageSize === pageSize) return
    setPage((current) => Math.floor(((current - 1) * previousPageSize) / pageSize) + 1)
    previousPageSizeRef.current = pageSize
  }, [pageSize])
  useEffect(() => {
    setPage((current) => Math.min(current, pageCount))
  }, [pageCount])
  const authorSections = useMemo(() => {
    if (groupMode !== 'author' || selectedId !== null) return []
    const grouped = new Map<string, { id: number, name: string, works: WorkGroup[] }>()
    for (const work of paginatedWorks) {
      const workAuthors = work.authors.length ? work.authors : [{ id: 0, name: '未关联作者' }]
      for (const author of workAuthors) {
        const key = `${author.id}:${author.name}`
        let section = grouped.get(key)
        if (!section) {
          section = { ...author, works: [] }
          grouped.set(key, section)
        }
        section.works.push(work)
      }
    }
    return [...grouped.values()].sort((left, right) => workTitleCollator.compare(left.name, right.name))
  }, [groupMode, paginatedWorks, selectedId])
  const failedJob = useMemo(() => jobs.find((job) => (
    job.status === 'failed'
    && job.id > dismissedJobThrough
    && !jobs.some((newer) => newer.id > job.id && newer.status === 'succeeded' && jobSubject(newer) === jobSubject(job))
  )), [dismissedJobThrough, jobs])
  const enabledProviders = useMemo(() => sources.map((source) => source.name), [sources])
  const readyDownloadJob = useMemo(
    () => jobs.find((job) => job.kind === 'download_chapter' && job.status === 'succeeded' && job.payload.output_path),
    [jobs],
  )

  useEffect(() => {
    if (!(socialStatus?.processing_pending)) return
    let cancelled = false
    const timer = window.setInterval(() => {
      void api.socialStatus().then((next) => {
        if (!cancelled) {
          setSocialStatus((current) => (
            current
            && current.processing_pending === next.processing_pending
            && current.processing_retry === next.processing_retry
            && current.unread_count === next.unread_count
            && current.pending_count === next.pending_count
              ? current
              : next
          ))
        }
      }).catch(() => { /* The next poll will retry. */ })
    }, 10000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [socialStatus?.processing_pending])

  function dismissFailedJob(id: number) {
    setDismissedJobThrough(id)
    try { window.sessionStorage.setItem('mangafinder:dismissed-job-through', String(id)) }
    catch { /* The banner can still be dismissed until the next page load. */ }
  }

  const act = useCallback(async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try { await action(); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败') }
    finally { setBusy(false) }
  }, [load])

  const actForAuthorEdit = useCallback(async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try { await action(); await load() }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败')
      throw reason
    }
    finally { setBusy(false) }
  }, [load])

  const openWork = useCallback(async (work: WorkGroup | number) => {
    void loadWorkDetail()
    setBusy(true)
    try { setOpenedWork(await api.workGroup(typeof work === 'number' ? work : work.id)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '加载版本失败') }
    finally { setBusy(false) }
  }, [])

  const handleOpenWork = useCallback((work: WorkGroup) => {
    void openWork(work)
  }, [openWork])

  async function downloadEdition(edition: Edition, source: WorkSource) {
    setBusy(true)
    setError(null)
    try {
      const check = await api.libraryCheck(edition.work_id, source.provider)
      const matches = check.matches.map((item) => `• ${item.title}（${item.reason}）`).join('\n')
      const detail = [check.message, matches].filter(Boolean).join('\n')
      if (!window.confirm(`${detail}\n\n${check.status === 'not_found' ? '继续检查下载来源？' : '仍要继续下载吗？'}`)) return
      const chapters = await api.chapters(edition.work_id, source.provider)
      if (!chapters.length) throw new Error('这个来源暂时没有可下载章节')
      const chapter = chapters[0]
      const label = chapter.number ? `第 ${chapter.number} 话` : (chapter.title ?? '最新章节')
      if (!window.confirm(`从 ${source.provider} 把《${edition.title}》${label}加入 CBZ 下载队列？`)) return
      await api.downloadChapter(edition.work_id, source.provider, chapter.external_id, check.status !== 'not_found')
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建下载任务失败')
    } finally {
      setBusy(false)
    }
  }

  async function updateMetadata(patch: WorkMetadataPatch): Promise<boolean> {
    if (!openedWork) return false
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updateWorkMetadata(openedWork.id, patch)
      setOpenedWork(updated)
      await load()
      return true
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存作品资料失败')
      return false
    } finally { setBusy(false) }
  }

  async function splitEdition(workId: number) {
    if (!openedWork || !window.confirm('确认把这个版本拆成一部独立作品？')) return
    await act(() => api.splitEdition(openedWork.id, workId))
    setOpenedWork(null)
  }

  async function mergeCurrentInto(targetGroupId: number) {
    if (!openedWork || !window.confirm('确认合并？所有版本都会保留，此关系会标记为人工决定。')) return
    await act(() => api.mergeGroups(targetGroupId, openedWork.id))
    setOpenedWork(null)
  }

  async function excludeEdition(authorId: number, workId: number) {
    if (!openedWork || !window.confirm('确认这个版本不是该作者的作品？移除后，同一来源不会在刷新时再次添加。')) return
    await act(() => api.excludeEdition(openedWork.id, workId, authorId))
    setOpenedWork(null)
  }

  async function excludeGroup(authorId: number) {
    if (!openedWork || !window.confirm('确认整部作品都不是该作者的作品？所有相关版本都会移除并加入排除名单。')) return
    await act(() => api.excludeWorkGroup(openedWork.id, authorId))
    setOpenedWork(null)
  }

  async function reviewSuggestion(id: number, accept: boolean) {
    await act(() => accept ? api.acceptSuggestion(id) : api.rejectSuggestion(id))
  }

  const changePage = useCallback((nextPage: number) => {
    const target = Math.max(1, Math.min(nextPage, pageCount))
    if (target === page) return
    setPage(target)
    window.requestAnimationFrame(() => catalogTopRef.current?.scrollIntoView?.({
      block: 'start',
      behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    }))
  }, [page, pageCount])

  const showDashboard = useCallback(() => {
    setSettingsOpen(false)
    setLibraryMode('overview')
    setSelectedId(null)
  }, [])

  const showTodo = useCallback(() => {
    setSettingsOpen(false)
    setLibraryMode('todo')
    setSelectedId(null)
  }, [])

  const showAllWorks = useCallback(() => {
    setSettingsOpen(false)
    setPage(1)
    setLibraryMode('works')
    setSelectedId(null)
  }, [])

  const showAuthorDirectory = useCallback(() => {
    setSettingsOpen(false)
    setPage(1)
    setLibraryMode('authors')
    setSelectedId(null)
  }, [])

  const openAuthorWorks = useCallback((authorId: number) => {
    setSettingsOpen(false)
    setPage(1)
    setPreviewAuthorId(authorId)
    setLibraryMode('works')
    setSelectedId(authorId)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  const handleSidebarSelect = useCallback((id: number | null) => {
    if (id === null) showAllWorks()
    else openAuthorWorks(id)
  }, [openAuthorWorks, showAllWorks])

  const handleModeChange = useCallback((mode: LibraryMode) => {
    if (mode === 'overview') showDashboard()
    else if (mode === 'todo') showTodo()
    else if (mode === 'authors') showAuthorDirectory()
    else showAllWorks()
  }, [showAllWorks, showAuthorDirectory, showDashboard, showTodo])

  const handleAddAuthor = useCallback(
    (name: string, aliases?: string[]) => actForAuthorEdit(() => api.createAuthor(name, aliases)),
    [actForAuthorEdit],
  )
  const handleRefreshAuthor = useCallback(
    (id: number) => act(() => api.refreshAuthor(id)),
    [act],
  )
  const handleDeleteAuthor = useCallback(async (id: number) => {
    if (!window.confirm('删除这个作者订阅？已发现的作品资料会保留。')) return
    setSelectedId((current) => current === id ? null : current)
    await act(() => api.deleteAuthor(id))
  }, [act])
  const handleOpenSettings = useCallback(() => {
    setAssistantOpen(false)
    setSettingsOpen(true)
  }, [])
  const handleOpenRadar = useCallback(() => setRadarOpen(true), [])
  const handleOpenAssistant = useCallback(() => setAssistantOpen(true), [])
  const handleOpenHistory = useCallback(() => {
    setSettingsOpen(false)
    setHistoryOpen(true)
  }, [])
  const handleOpenReviews = useCallback(() => setReviewOpen(true), [])
  const handleReload = useCallback(() => { void load() }, [load])
  const handleDashboardOpenWork = useCallback((id: number) => { void openWork(id) }, [openWork])
  const handleSetEventState = useCallback(
    (id: number, state: CatalogEvent['state']) => { void act(() => api.updateDiscoveryEvent(id, state)) },
    [act],
  )
  const handleReadAllEvents = useCallback(
    () => { void act(() => api.updateAllDiscoveryEvents()) },
    [act],
  )
  const handleUpdateAuthorIdentity = useCallback(
    (id: number, identity: Pick<Author, 'name'> & Partial<Pick<Author, 'aliases' | 'circle_names'>>) =>
      actForAuthorEdit(() => api.updateAuthorIdentity(id, identity)),
    [actForAuthorEdit],
  )
  const handleFilterChange = useCallback((value: WorkFilter) => { setWorkFilter(value); setPage(1) }, [])
  const handleSortChange = useCallback((value: WorkSort) => { setWorkSort(value); setPage(1) }, [])
  const handleGroupChange = useCallback((value: GroupMode) => { setGroupMode(value); setPage(1) }, [])
  const coverWallStyle = useMemo(() => ({ gridTemplateColumns: `repeat(${coverColumns}, minmax(0, 1fr))` }), [coverColumns])

  function renderWorks(items: WorkGroup[]) {
    if (viewMode === 'covers') {
      return <section className="cover-wall" style={coverWallStyle}>{items.map((work) => <WorkCoverTile work={work} onOpen={handleOpenWork} key={work.id} />)}</section>
    }
    if (viewMode === 'list') {
      return <section className="work-list">{items.map((work) => <WorkListRow work={work} onOpen={handleOpenWork} key={work.id} />)}</section>
    }
    return <section className="work-grid">{items.map((work) => <WorkCard work={work} onOpen={handleOpenWork} key={work.id} />)}</section>
  }

  return (
    <div className={`shell${sidebarExpanded ? '' : ' sidebar-collapsed'}${hasModal ? ' modal-active' : ''}`}>
      <AuthorSidebar
        authors={authors}
        sources={sources}
        selectedId={settingsOpen ? -1 : selectedId}
        libraryMode={libraryMode}
        busy={busy}
        onExpandedChange={setSidebarExpanded}
        onSelect={handleSidebarSelect}
        onModeChange={handleModeChange}
        onAdd={handleAddAuthor}
        onRefresh={handleRefreshAuthor}
        onDelete={handleDeleteAuthor}
        settingsActive={settingsOpen}
        onOpenSettings={handleOpenSettings}
        onOpenRadar={handleOpenRadar}
        onOpenAssistant={handleOpenAssistant}
        onOpenHistory={handleOpenHistory}
      />

      <main className={settingsOpen ? 'settings-main' : libraryMode === 'authors' ? 'author-directory-main' : libraryMode === 'overview' || libraryMode === 'todo' ? 'dashboard-main' : ''}>
        {error && <div className="alert" role="alert">
          <div className="alert-copy"><strong>操作失败</strong><span>{error}</span></div>
          <button className="alert-close" onClick={() => setError(null)} aria-label="关闭错误提示">×</button>
        </div>}
        {settingsOpen
          ? <Suspense fallback={<section className="settings-loading"><span className="spinner" /> 正在读取系统设置…</section>}><SystemSettings onClose={() => setSettingsOpen(false)} /></Suspense>
          : libraryMode === 'todo'
            ? <Suspense fallback={<section className="settings-loading"><span className="spinner" /> 正在加载待办工作台…</section>}><TodoWorkbench works={works} onOpenWork={handleDashboardOpenWork} onOpenReviews={handleOpenReviews} onChanged={handleReload} /></Suspense>
          : libraryMode === 'overview'
            ? dashboard
              ? <Dashboard
                  data={dashboard}
                  events={discoveryEvents}
                  busy={busy}
                  onReload={handleReload}
                  onOpenWork={handleDashboardOpenWork}
                  onOpenRadar={handleOpenRadar}
                  onSetEventState={handleSetEventState}
                  onReadAll={handleReadAllEvents}
                  onOpenReviews={handleOpenReviews}
                />
              : <section className="settings-loading"><span className="spinner" /> 正在读取资料库总览…</section>
            : libraryMode === 'authors'
              ? <Suspense fallback={<section className="settings-loading"><span className="spinner" /> 正在加载作者目录…</section>}><AuthorDirectory
                  authors={authors}
                  works={works}
                  previewAuthorId={previewAuthorId}
                  onPreviewAuthor={setPreviewAuthorId}
                  onOpenAuthor={openAuthorWorks}
                  onGetQueryName={api.authorQueryName}
                  onUpdateAuthor={handleUpdateAuthorIdentity}
                /></Suspense>
              : <>
        <header className="topbar">
          <div>
            <p className="eyebrow">LIBRARY / DISCOVERY</p>
            <h1>{selectedAuthor?.name ?? '作品总览'}</h1>
            <p>{selectedAuthor ? '这个作者在所有来源中的作品' : '你订阅作者的作品，都在这里。'}</p>
          </div>
          <div className="topbar-tools">
            <button className="radar-button" onClick={() => setImageSearchOpen(true)}>以图搜库</button>
            <button className="radar-button" onClick={() => setRadarOpen(true)}>动态雷达{socialStatus?.unread_count ? <span>{socialStatus.unread_count}</span> : null}</button>
            <div className="search-box">
              <span aria-hidden="true">⌕</span>
              <input value={search} onChange={(event) => { setSearch(event.target.value); setPage(1) }} placeholder="搜索作品、作者或标签" aria-label="搜索作品" />
            </div>
          </div>
        </header>

        {failedJob && <div className="alert" role="alert">
          <div className="alert-copy">
            <strong>{JOB_LABELS[failedJob.kind] ?? '后台任务'}失败</strong>
            <span>{friendlyJobError(failedJob.error)}</span>
            <small>{formatJobTime(failedJob.finished_at ?? failedJob.started_at ?? failedJob.created_at)} · 已尝试 {failedJob.attempts} 次</small>
          </div>
          <button className="alert-close" onClick={() => dismissFailedJob(failedJob.id)} aria-label="关闭任务失败提示">×</button>
        </div>}
        {activeJobs.length > 0 && <div className="sync-banner"><span className="spinner" /> 正在向来源查询，作品会自动出现…</div>}
        {suggestions.length > 0 && <button className="review-banner" onClick={() => setReviewOpen(true)}>有 {suggestions.length} 个相似作品等待确认聚合 <span>去审核 →</span></button>}
        {seriesCandidates.slice(0, 1).map((candidate) => <button className="review-banner" key={`${candidate.normalized_title}:${candidate.author_ids.join('-')}`} onClick={() => {
          const gap = candidate.missing_volumes.length ? `，可能缺少第 ${candidate.missing_volumes.join('、')} 卷` : ''
          if (window.confirm(`将 ${candidate.groups.length} 部作品建立为“${candidate.title}”系列${gap}？`)) void act(() => api.createSeriesFromCandidate(candidate))
        }}>发现系列候选「{candidate.title}」 · {candidate.groups.length} 卷{candidate.missing_volumes.length ? ` · 缺 ${candidate.missing_volumes.join('、')}` : ''}<span>确认系列 →</span></button>)}
        {readyDownloadJob && (
          <div className="download-ready" key={readyDownloadJob.id}>CBZ 已准备好 <a href={`/api/jobs/${readyDownloadJob.id}/file`}>保存到本机 ↓</a></div>
        )}

        <section className="section-head">
          <div><h2>发现的作品</h2><span>{visibleWorks.length}</span></div>
          <button onClick={() => void load()} disabled={busy}>刷新视图</button>
        </section>

        {works.length > 0 && <section className="catalog-toolbar" aria-label="作品显示设置" ref={catalogTopRef}>
          <div className="view-switch" role="group" aria-label="显示方式">
            {([['cards', '信息卡'], ['covers', '封面墙'], ['list', '紧凑列表']] as const).map(([value, label]) => (
              <button className={viewMode === value ? 'active' : ''} onClick={() => setViewMode(value)} aria-pressed={viewMode === value} key={value}>{label}</button>
            ))}
          </div>
          <ThemedSelect label="筛选" value={workFilter} options={workFilterOptions} onChange={handleFilterChange} />
          <ThemedSelect label="排序" value={workSort} options={workSortOptions} onChange={handleSortChange} />
          {!selectedAuthor && <ThemedSelect label="分组" value={groupMode} options={groupOptions} onChange={handleGroupChange} />}
          {visibleWorks.length > 0 && <span className="catalog-page-summary">第 {page} / {pageCount} 页 · 共 {visibleWorks.length} 部</span>}
        </section>}

        {visibleWorks.length > 0 ? <>
          {authorSections.length > 0 ? (
            <div className="author-shelves">
              {authorSections.map((section) => <section className="author-shelf" key={section.id}>
                <header><div><span>{section.name.slice(0, 1).toLocaleUpperCase()}</span><h3>{section.name}</h3></div><small>{section.works.length} 部作品</small></header>
                {renderWorks(section.works)}
              </section>)}
            </div>
          ) : renderWorks(paginatedWorks)}
          {pageCount > 1 && <nav className="catalog-pagination" aria-label="作品分页">
            <div><strong>浏览作品</strong><span>共 {pageCount} 页 · {visibleWorks.length} 部作品</span></div>
            <div className="pagination-buttons">
              <button onClick={() => changePage(1)} disabled={page === 1} aria-label="第一页">首页</button>
              <button onClick={() => changePage(page - 1)} disabled={page === 1} aria-label="上一页">上一页</button>
              <PageSelector page={page} pageCount={pageCount} totalItems={visibleWorks.length} onChange={changePage} />
              <button onClick={() => changePage(page + 1)} disabled={page === pageCount} aria-label="下一页">下一页</button>
              <button onClick={() => changePage(pageCount)} disabled={page === pageCount} aria-label="最后一页">末页</button>
            </div>
          </nav>}
        </> : (
          <section className="empty-state">
            <div className="empty-symbol">冊</div>
            <h2>{works.length ? '当前条件下没有作品' : authors.length ? '还没有发现作品' : '从一位作者开始'}</h2>
            <p>{works.length ? '可以清除搜索词或切换筛选条件。' : authors.length ? '来源查询可能仍在进行，也可以点击作者旁的刷新按钮重试。' : '在左侧输入作者名，我们会在已启用来源中建立他的作品档案。'}</p>
          </section>
        )}
            </>}
      </main>
      <Suspense fallback={null}>
        {!settingsOpen && historyOpen && <OperationHistory onClose={() => setHistoryOpen(false)} onChanged={async () => { setOpenedWork(null); await load() }} />}
        {!settingsOpen && imageSearchOpen && <ImageSearch onClose={() => setImageSearchOpen(false)} onOpenWork={(id) => { setImageSearchOpen(false); void openWork(id) }} />}
        {!settingsOpen && openedWork && <WorkDetail group={openedWork} allGroups={works} busy={busy} enabledProviders={enabledProviders} activeAuthorId={selectedId} onClose={() => setOpenedWork(null)} onDownload={(edition, source) => void downloadEdition(edition, source)} onUpdateMetadata={updateMetadata} onSplit={(workId) => void splitEdition(workId)} onMerge={(targetId) => void mergeCurrentInto(targetId)} onExcludeEdition={(authorId, workId) => void excludeEdition(authorId, workId)} onExcludeGroup={(authorId) => void excludeGroup(authorId)} />}
        {!settingsOpen && reviewOpen && <MergeReview suggestions={suggestions} agentStatus={agentStatus} busy={busy} onRunAgent={() => void act(() => api.runAgentReviews())} onOpenGroup={(id) => void openWork(id)} onClose={() => setReviewOpen(false)} onAccept={(id) => void reviewSuggestion(id, true)} onReject={(id) => void reviewSuggestion(id, false)} />}
        {!settingsOpen && radarOpen && socialStatus && <SocialRadar status={socialStatus} signals={socialSignals} authors={authors} works={works} selectedAuthorId={selectedId} busy={busy} focusSignalId={focusSignalId} onClose={() => setRadarOpen(false)} onChanged={load} onOpenWork={(id) => { setRadarOpen(false); void openWork(id) }} />}
        {assistantOpen && <AssistantChat open={assistantOpen} onClose={() => setAssistantOpen(false)} />}
      </Suspense>
    </div>
  )
}
