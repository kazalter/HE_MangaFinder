import {
  ArrowsClockwise,
  BookOpenText,
  CaretDoubleLeft,
  CaretDoubleRight,
  ChartBar,
  Check,
  GearSix,
  ListMagnifyingGlass,
  MagnifyingGlass,
  Plus,
  PushPin,
  SortAscending,
  Sparkle,
  SquaresFour,
  Trash,
  User,
  X,
} from '@phosphor-icons/react'
import { memo, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import type { LibraryMode } from '../App'
import mangaFinderMark from '../assets/mangafinder-mark.png'
import type { Author, Source } from '../types'
import { ExpandablePanel } from './ExpandablePanel'

const sidebarExpandedKey = 'mangafinder:library-sidebar-expanded'
const pinnedAuthorsKey = 'mangafinder:pinned-authors'
const authorCollator = new Intl.Collator('zh-CN')

function initialSidebarExpanded() {
  try { return localStorage.getItem(sidebarExpandedKey) !== 'false' }
  catch { return true }
}

function initialPinnedAuthors(): number[] | null {
  try {
    const saved = localStorage.getItem(pinnedAuthorsKey)
    if (saved === null) return null
    const value = JSON.parse(saved)
    return Array.isArray(value) ? value.filter((id): id is number => Number.isInteger(id)).slice(0, 8) : null
  } catch {
    return null
  }
}

interface Props {
  authors: Author[]
  sources: Source[]
  selectedId: number | null
  libraryMode: LibraryMode
  todoCount?: number
  busy: boolean
  onSelect: (id: number | null) => void
  onModeChange: (mode: LibraryMode) => void
  onAdd: (name: string, aliases?: string[]) => Promise<void>
  onRefresh: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
  settingsActive?: boolean
  onExpandedChange?: (expanded: boolean) => void
  onOpenSettings: () => void
  onOpenAssistant?: () => void
}

interface SidebarAuthorRowProps {
  author: Author
  pinned?: boolean
  active: boolean
  isPinned: boolean
  managing: boolean
  onSelect: (id: number) => void
  onTogglePinned: (id: number) => void
  onRefresh: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
}

const SidebarAuthorRow = memo(function SidebarAuthorRow({
  author,
  pinned = false,
  active,
  isPinned,
  managing,
  onSelect,
  onTogglePinned,
  onRefresh,
  onDelete,
}: SidebarAuthorRowProps) {
  return (
    <div className={`library-author-row ${pinned ? 'pinned' : ''} ${active ? 'active' : ''}`}>
      <button className="library-author-main" onClick={() => onSelect(author.id)} aria-current={active ? 'page' : undefined}>
        <span className="library-avatar">
          {author.avatar_url
            ? <img src={author.avatar_url} alt="" referrerPolicy="no-referrer" loading="lazy" decoding="async" />
            : author.name.slice(0, 1).toLocaleUpperCase()}
        </span>
        <span>{author.name}</span>
        <small>{author.work_count}</small>
      </button>
      <div className={`library-author-actions ${managing ? 'visible' : ''}`}>
        <button onClick={() => onTogglePinned(author.id)} aria-label={`${isPinned ? '取消置顶' : '置顶'} ${author.name}`} title={isPinned ? '取消置顶' : '置顶'}>
          <PushPin size={14} weight={isPinned ? 'fill' : 'regular'} />
        </button>
        {managing && <>
          <button onClick={() => void onRefresh(author.id)} aria-label={`刷新 ${author.name}`} title="立即刷新"><ArrowsClockwise size={14} /></button>
          <button onClick={() => void onDelete(author.id)} aria-label={`删除 ${author.name}`} title="删除订阅"><Trash size={14} /></button>
        </>}
      </div>
    </div>
  )
})

export const AuthorSidebar = memo(function AuthorSidebar({
  authors,
  sources,
  selectedId,
  libraryMode,
  todoCount = 0,
  busy,
  onSelect,
  onModeChange,
  onAdd,
  onRefresh,
  onDelete,
  settingsActive = false,
  onExpandedChange,
  onOpenSettings,
  onOpenAssistant,
}: Props) {
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [aliasesText, setAliasesText] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [managing, setManaging] = useState(false)
  const [expanded, setExpanded] = useState(initialSidebarExpanded)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [mobileViewport, setMobileViewport] = useState(() => window.matchMedia?.('(max-width: 800px)').matches ?? false)
  const [pinnedIds, setPinnedIds] = useState<number[] | null>(initialPinnedAuthors)
  const sidebarRef = useRef<HTMLElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const addInputRef = useRef<HTMLInputElement>(null)
  const expandButtonRef = useRef<HTMLButtonElement>(null)
  const collapseButtonRef = useRef<HTMLButtonElement>(null)

  const totalWorks = useMemo(() => authors.reduce((sum, author) => sum + author.work_count, 0), [authors])
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const filteredAuthors = useMemo(() => authors
    .filter((author) => !normalizedQuery || author.name.toLocaleLowerCase().includes(normalizedQuery))
    .sort((left, right) => authorCollator.compare(left.name, right.name)), [authors, normalizedQuery])
  const effectivePinnedIds = useMemo(
    () => pinnedIds ?? authors.slice(0, 3).map((author) => author.id),
    [authors, pinnedIds],
  )
  const pinnedSet = useMemo(() => new Set(effectivePinnedIds), [effectivePinnedIds])
  const pinnedAuthors = useMemo(() => {
    const byId = new Map(authors.map((author) => [author.id, author]))
    return effectivePinnedIds
      .map((id) => byId.get(id))
      .filter((author): author is Author => Boolean(author))
      .slice(0, 3)
  }, [authors, effectivePinnedIds])
  const groupedAuthors = useMemo(() => {
    const groups = new Map<string, Author[]>()
    for (const author of filteredAuthors) {
      const first = author.name.trim().charAt(0).toLocaleUpperCase()
      const key = /^[A-Z]$/.test(first) ? first : '#'
      let bucket = groups.get(key)
      if (!bucket) {
        bucket = []
        groups.set(key, bucket)
      }
      bucket.push(author)
    }
    return [...groups.entries()].sort(([left], [right]) => {
      if (left === '#') return 1
      if (right === '#') return -1
      return left < right ? -1 : left > right ? 1 : 0
    })
  }, [filteredAuthors])

  useEffect(() => {
    try { localStorage.setItem(sidebarExpandedKey, String(expanded)) }
    catch { /* The navigation remains functional without preference storage. */ }
  }, [expanded])

  useEffect(() => {
    if (!addOpen) return
    window.requestAnimationFrame(() => addInputRef.current?.focus())
  }, [addOpen])

  useEffect(() => {
    if (pinnedIds === null) return
    try { localStorage.setItem(pinnedAuthorsKey, JSON.stringify(pinnedIds)) }
    catch { /* Pins remain available for this session. */ }
  }, [pinnedIds])

  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 800px)')
    if (!media) return
    const updateViewport = () => setMobileViewport(media.matches)
    updateViewport()
    media.addEventListener?.('change', updateViewport)
    return () => media.removeEventListener?.('change', updateViewport)
  }, [])

  useEffect(() => {
    if (!mobileOpen) return
    const sidebar = sidebarRef.current
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusable = sidebar?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')
    focusable?.[0]?.focus()
    document.body.classList.add('sidebar-drawer-open')

    function handleKeydown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setMobileOpen(false)
        return
      }
      if (event.key !== 'Tab' || !focusable?.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeydown)
    return () => {
      document.removeEventListener('keydown', handleKeydown)
      document.body.classList.remove('sidebar-drawer-open')
      previousFocus?.focus()
    }
  }, [mobileOpen])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return
    const aliases = aliasesText
      .split(/[,，、\n]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    try { await onAdd(name.trim(), aliases) }
    catch { return }
    setName('')
    setAliasesText('')
    setAddOpen(false)
  }

  function isMobileViewport() {
    return window.matchMedia?.('(max-width: 800px)').matches ?? false
  }

  const selectAuthor = useCallback((id: number | null) => {
    onSelect(id)
    if (isMobileViewport()) setMobileOpen(false)
  }, [onSelect])

  function navigateMode(mode: LibraryMode) {
    onModeChange(mode)
    if (isMobileViewport()) setMobileOpen(false)
  }

  const togglePinned = useCallback((authorId: number) => {
    setPinnedIds((current) => {
      const base = current ?? authors.slice(0, 3).map((author) => author.id)
      return base.includes(authorId) ? base.filter((id) => id !== authorId) : [authorId, ...base].slice(0, 8)
    })
  }, [authors])

  function updateSidebarState(nextExpanded: boolean, focusTarget: () => HTMLElement | null) {
    setExpanded(nextExpanded)
    onExpandedChange?.(nextExpanded)
    window.requestAnimationFrame(() => focusTarget()?.focus())
  }

  function collapseSidebar() {
    updateSidebarState(false, () => expandButtonRef.current)
  }

  function expandSidebar() {
    updateSidebarState(true, () => collapseButtonRef.current)
  }

  const todoBadge = todoCount > 99 ? '99+' : todoCount > 0 ? String(todoCount) : null

  return (
    <>
      <button className="sidebar-mobile-trigger" onClick={() => setMobileOpen(true)} aria-label="打开资料库导航">
        <BookOpenText size={21} weight="bold" />
      </button>
      <button className={`sidebar-mobile-backdrop ${mobileOpen ? 'visible' : ''}`} onClick={() => setMobileOpen(false)} aria-label="关闭资料库导航" />

      <aside
        ref={sidebarRef}
        className={`library-sidebar ${expanded ? 'expanded' : 'collapsed'} ${mobileOpen ? 'mobile-open' : ''}`}
        aria-label="资料库导航"
        aria-hidden={mobileViewport ? !mobileOpen : false}
      >
        <div className="sidebar-expanded-content" aria-hidden={mobileViewport ? undefined : !expanded}>
          <header className="library-brand-row">
            <span className="library-brand-mark" aria-hidden="true">
              <img src={mangaFinderMark} alt="" width={46} height={46} decoding="async" />
            </span>
            <div><strong>MangaFinder</strong><small>作者作品雷达</small></div>
            <button
              ref={collapseButtonRef}
              onClick={mobileViewport ? () => setMobileOpen(false) : collapseSidebar}
              aria-label={mobileViewport ? '关闭资料库导航' : '收起资料库导航'}
            >
              {mobileViewport ? <X size={20} /> : <CaretDoubleLeft size={21} />}
            </button>
          </header>

          <nav className="library-tabs" aria-label="主导航">
            <button
              className={libraryMode === 'workspace' && !settingsActive ? 'active' : ''}
              onClick={() => navigateMode('workspace')}
            >
              工作台{todoBadge ? <> <span>{todoBadge}</span></> : null}
            </button>
            <button
              className={libraryMode === 'works' && !settingsActive ? 'active' : ''}
              onClick={() => navigateMode('works')}
            >
              作品 <span>{totalWorks}</span>
            </button>
            <button
              className={libraryMode === 'authors' && !settingsActive ? 'active' : ''}
              onClick={() => navigateMode('authors')}
            >
              作者 <span>{authors.length}</span>
            </button>
          </nav>

          <section className="library-find-author">
            <div className="library-filter-heading">
              <h2>按作者浏览作品</h2>
              {libraryMode === 'works' && selectedId !== null && (
                <button className="library-filter-reset" onClick={() => selectAuthor(null)}>查看全部 ({totalWorks})</button>
              )}
            </div>
            <div className="library-find-row">
              <label>
                <MagnifyingGlass size={16} />
                <span className="sr-only">搜索作者</span>
                <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索或筛选作者…" />
              </label>
              <button className={addOpen ? 'active' : ''} onClick={() => setAddOpen((value) => !value)} aria-label={addOpen ? '取消添加作者' : '添加作者'} aria-expanded={addOpen} aria-controls="library-add-panel">
                <Plus size={20} />
              </button>
            </div>
            <ExpandablePanel open={addOpen} className="library-add-panel">
              <form className="library-add-form" id="library-add-panel" onSubmit={submit}>
                <input ref={addInputRef} value={name} onChange={(event) => setName(event.target.value)} placeholder="输入要订阅的作者名" maxLength={200} />
                <input value={aliasesText} onChange={(event) => setAliasesText(event.target.value)} placeholder="备选名 / 别名（逗号分隔，如：pija, ピアニッシモ）" maxLength={500} />
                <button disabled={busy || !name.trim()}>确认添加</button>
              </form>
            </ExpandablePanel>
          </section>

          <div className="library-author-scroll">
            {!normalizedQuery && <section className="library-author-section pinned-authors">
              <header><h2>置顶作者</h2><PushPin size={15} weight="fill" /></header>
              {pinnedAuthors.length ? pinnedAuthors.map((author) => (
                <SidebarAuthorRow
                  key={`pinned-${author.id}`}
                  author={author}
                  pinned
                  active={libraryMode === 'works' && selectedId === author.id}
                  isPinned={pinnedSet.has(author.id)}
                  managing={managing}
                  onSelect={selectAuthor}
                  onTogglePinned={togglePinned}
                  onRefresh={onRefresh}
                  onDelete={onDelete}
                />
              )) : <p className="library-empty">从作者列表中选择置顶</p>}
            </section>}

            <section className="library-author-section all-library-authors">
              <header>
                <div><h2>{normalizedQuery ? '搜索结果' : '所有作者'}</h2><span>{filteredAuthors.length}</span></div>
                <div className="library-list-tools">
                  <span title="按名称排序"><SortAscending size={15} /></span>
                  <button className={managing ? 'active' : ''} onClick={() => setManaging((value) => !value)} aria-pressed={managing}>
                    {managing ? <Check size={14} /> : <ListMagnifyingGlass size={14} />}{managing ? '完成' : '管理'}
                  </button>
                </div>
              </header>
              {groupedAuthors.length ? groupedAuthors.map(([letter, items]) => <section className="library-letter-group" key={letter}>
                <h3>{letter}</h3>
                {items.map((author) => (
                  <SidebarAuthorRow
                    key={`all-${author.id}`}
                    author={author}
                    active={libraryMode === 'works' && selectedId === author.id}
                    isPinned={pinnedSet.has(author.id)}
                    managing={managing}
                    onSelect={selectAuthor}
                    onTogglePinned={togglePinned}
                    onRefresh={onRefresh}
                    onDelete={onDelete}
                  />
                ))}
              </section>) : <p className="library-empty">没有匹配的作者</p>}
            </section>
          </div>

          <footer className="library-sidebar-footer">
            <nav className="library-quick-actions" aria-label="全局工具">
              <button className="library-assistant-action" onClick={() => { onOpenAssistant?.(); setMobileOpen(false) }}><Sparkle size={18} /><span>AI 助手</span></button>
              <button className={settingsActive ? 'active' : ''} onClick={() => { onOpenSettings(); setMobileOpen(false) }}><GearSix size={18} /><span>系统设置</span></button>
            </nav>
            <div className="library-source-status"><span className="status-dot" /><div><b>{sources.length} 个来源已启用</b><small>{sources.map((source) => source.display_name).join(' · ') || '正在连接'}</small></div></div>
          </footer>
        </div>

        <div className="sidebar-collapsed-content" aria-hidden={mobileViewport ? true : expanded}>
          <span className="library-brand-mark" aria-label="MangaFinder">
            <img src={mangaFinderMark} alt="" width={42} height={42} decoding="async" />
          </span>
          <button ref={expandButtonRef} onClick={expandSidebar} aria-label="展开资料库导航" title="展开资料库导航"><CaretDoubleRight size={21} /></button>
          <nav aria-label="快捷导航">
            <button className={libraryMode === 'workspace' && !settingsActive ? 'active' : ''} onClick={() => navigateMode('workspace')} aria-label="工作台" title="工作台">
              <ChartBar size={21} />
              {todoBadge && <em className="collapsed-badge">{todoBadge}</em>}
            </button>
            <button className={libraryMode === 'works' && !settingsActive ? 'active' : ''} onClick={() => navigateMode('works')} aria-label="作品库" title="作品库"><SquaresFour size={21} /></button>
            <button className={libraryMode === 'authors' && !settingsActive ? 'active' : ''} onClick={() => navigateMode('authors')} aria-label="作者中心" title="作者中心"><User size={22} /></button>
          </nav>
          <div>
            <button onClick={() => onOpenAssistant?.()} aria-label="AI 助手" title="AI 助手"><Sparkle size={21} /></button>
            <button className={settingsActive ? 'active' : ''} onClick={onOpenSettings} aria-label="系统设置" title="系统设置"><GearSix size={22} /></button>
            <span className="collapsed-source-status" title={`${sources.length} 个来源已启用`}><span className="status-dot" /></span>
          </div>
        </div>
      </aside>
    </>
  )
})
