import {
  ArrowsClockwise,
  BookOpenText,
  CaretDoubleLeft,
  CaretDoubleRight,
  Check,
  Crosshair,
  GearSix,
  ListMagnifyingGlass,
  MagnifyingGlass,
  Plus,
  PushPin,
  SortAscending,
  SquaresFour,
  Trash,
  User,
  X,
} from '@phosphor-icons/react'
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import type { LibraryMode } from '../App'
import type { Author, Source } from '../types'

const sidebarExpandedKey = 'mangafinder:library-sidebar-expanded'
const pinnedAuthorsKey = 'mangafinder:pinned-authors'

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
  busy: boolean
  onSelect: (id: number | null) => void
  onModeChange: (mode: LibraryMode) => void
  onAdd: (name: string) => Promise<void>
  onRefresh: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
  settingsActive?: boolean
  onOpenSettings: () => void
  onOpenRadar: () => void
}

export function AuthorSidebar({
  authors,
  sources,
  selectedId,
  libraryMode,
  busy,
  onSelect,
  onModeChange,
  onAdd,
  onRefresh,
  onDelete,
  settingsActive = false,
  onOpenSettings,
  onOpenRadar,
}: Props) {
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [managing, setManaging] = useState(false)
  const [expanded, setExpanded] = useState(initialSidebarExpanded)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [mobileViewport, setMobileViewport] = useState(() => window.matchMedia?.('(max-width: 800px)').matches ?? false)
  const [pinnedIds, setPinnedIds] = useState<number[] | null>(initialPinnedAuthors)
  const sidebarRef = useRef<HTMLElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const expandButtonRef = useRef<HTMLButtonElement>(null)
  const collapseButtonRef = useRef<HTMLButtonElement>(null)

  const totalWorks = authors.reduce((sum, author) => sum + author.work_count, 0)
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const filteredAuthors = useMemo(() => authors
    .filter((author) => !normalizedQuery || author.name.toLocaleLowerCase().includes(normalizedQuery))
    .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN')), [authors, normalizedQuery])
  const effectivePinnedIds = pinnedIds ?? authors.slice(0, 3).map((author) => author.id)
  const pinnedAuthors = effectivePinnedIds
    .map((id) => authors.find((author) => author.id === id))
    .filter((author): author is Author => Boolean(author))
    .slice(0, 3)
  const groupedAuthors = useMemo(() => {
    const groups = new Map<string, Author[]>()
    for (const author of filteredAuthors) {
      const first = author.name.trim().charAt(0).toLocaleUpperCase()
      const key = /^[A-Z]$/.test(first) ? first : '#'
      groups.set(key, [...(groups.get(key) ?? []), author])
    }
    return [...groups.entries()].sort(([left], [right]) => {
      if (left === '#') return 1
      if (right === '#') return -1
      return left.localeCompare(right)
    })
  }, [filteredAuthors])

  useEffect(() => {
    try { localStorage.setItem(sidebarExpandedKey, String(expanded)) }
    catch { /* The navigation remains functional without preference storage. */ }
  }, [expanded])

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
    await onAdd(name.trim())
    setName('')
    setAddOpen(false)
  }

  function isMobileViewport() {
    return window.matchMedia?.('(max-width: 800px)').matches ?? false
  }

  function selectAuthor(id: number | null) {
    onSelect(id)
    if (isMobileViewport()) setMobileOpen(false)
  }

  function focusAuthors() {
    onModeChange('authors')
    if (isMobileViewport()) setMobileOpen(true)
    window.requestAnimationFrame(() => searchRef.current?.focus())
  }

  function togglePinned(authorId: number) {
    setPinnedIds((current) => {
      const base = current ?? authors.slice(0, 3).map((author) => author.id)
      return base.includes(authorId) ? base.filter((id) => id !== authorId) : [authorId, ...base].slice(0, 8)
    })
  }

  function collapseSidebar() {
    setExpanded(false)
    window.requestAnimationFrame(() => expandButtonRef.current?.focus())
  }

  function expandSidebar() {
    setExpanded(true)
    window.requestAnimationFrame(() => collapseButtonRef.current?.focus())
  }

  function authorRow(author: Author, pinned = false) {
    const active = selectedId === author.id
    return (
      <div className={`library-author-row ${pinned ? 'pinned' : ''} ${active ? 'active' : ''}`} key={`${pinned ? 'pinned' : 'all'}-${author.id}`}>
        <button className="library-author-main" onClick={() => selectAuthor(author.id)} aria-current={active ? 'page' : undefined}>
          <span className="library-avatar">
            {author.avatar_url
              ? <img src={author.avatar_url} alt="" referrerPolicy="no-referrer" />
              : author.name.slice(0, 1).toLocaleUpperCase()}
          </span>
          <span>{author.name}</span>
          <small>{author.work_count}</small>
        </button>
        <div className={`library-author-actions ${managing ? 'visible' : ''}`}>
          <button onClick={() => togglePinned(author.id)} aria-label={`${effectivePinnedIds.includes(author.id) ? '取消置顶' : '置顶'} ${author.name}`} title={effectivePinnedIds.includes(author.id) ? '取消置顶' : '置顶'}>
            <PushPin size={14} weight={effectivePinnedIds.includes(author.id) ? 'fill' : 'regular'} />
          </button>
          {managing && <>
            <button onClick={() => void onRefresh(author.id)} aria-label={`刷新 ${author.name}`} title="立即刷新"><ArrowsClockwise size={14} /></button>
            <button onClick={() => void onDelete(author.id)} aria-label={`删除 ${author.name}`} title="删除订阅"><Trash size={14} /></button>
          </>}
        </div>
      </div>
    )
  }

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
        <div className="sidebar-expanded-content">
          <header className="library-brand-row">
            <span className="library-brand-mark" aria-hidden="true">漫</span>
            <div><strong>MangaFinder</strong><small>作者作品雷达</small></div>
            <button
              ref={collapseButtonRef}
              onClick={mobileViewport ? () => setMobileOpen(false) : collapseSidebar}
              aria-label={mobileViewport ? '关闭资料库导航' : '收起资料库导航'}
            >
              {mobileViewport ? <X size={20} /> : <CaretDoubleLeft size={21} />}
            </button>
          </header>

          <div className="library-tabs" role="group" aria-label="资料库范围">
            <button className={libraryMode === 'works' && !settingsActive ? 'active' : ''} onClick={() => onModeChange('works')}>作品</button>
            <button className={libraryMode === 'authors' && !settingsActive ? 'active' : ''} onClick={focusAuthors}>作者 <span>{authors.length}</span></button>
          </div>

          <button className={`library-all-works ${libraryMode === 'works' && selectedId === null && !settingsActive ? 'active' : ''}`} onClick={() => selectAuthor(null)}>
            <SquaresFour size={18} weight="fill" /><b>全部作品</b><span>{totalWorks}</span>
          </button>

          <section className="library-find-author">
            <h2>查找作者</h2>
            <div className="library-find-row">
              <label>
                <MagnifyingGlass size={16} />
                <span className="sr-only">搜索作者</span>
                <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：浅野一二〇" />
              </label>
              <button className={addOpen ? 'active' : ''} onClick={() => setAddOpen((value) => !value)} aria-label={addOpen ? '取消添加作者' : '添加作者'} aria-expanded={addOpen}>
                {addOpen ? <X size={20} /> : <Plus size={20} />}
              </button>
            </div>
            {addOpen && <form className="library-add-form" onSubmit={submit}>
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="输入要订阅的作者名" maxLength={200} autoFocus />
              <button disabled={busy || !name.trim()}>确认添加</button>
            </form>}
          </section>

          <div className="library-author-scroll">
            {!normalizedQuery && <section className="library-author-section pinned-authors">
              <header><h2>置顶作者</h2><PushPin size={15} weight="fill" /></header>
              {pinnedAuthors.length ? pinnedAuthors.map((author) => authorRow(author, true)) : <p className="library-empty">从作者列表中选择置顶</p>}
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
                {items.map((author) => authorRow(author))}
              </section>) : <p className="library-empty">没有匹配的作者</p>}
            </section>
          </div>

          <footer className="library-sidebar-footer">
            <button className={settingsActive ? 'active' : ''} onClick={() => { onOpenSettings(); setMobileOpen(false) }}>
              <GearSix size={19} /><span><b>系统设置</b><small>连接、模型与通知</small></span><CaretDoubleRight size={16} />
            </button>
            <div className="library-source-status"><span className="status-dot" /><div><b>{sources.length} 个来源已启用</b><small>{sources.map((source) => source.display_name).join(' · ') || '正在连接'}</small></div></div>
          </footer>
        </div>

        <div className="sidebar-collapsed-content">
          <span className="library-brand-mark" aria-label="MangaFinder">漫</span>
          <button ref={expandButtonRef} onClick={expandSidebar} aria-label="展开资料库导航" title="展开资料库导航"><CaretDoubleRight size={21} /></button>
          <nav aria-label="快捷导航">
            <button className={libraryMode === 'works' && selectedId === null && !settingsActive ? 'active' : ''} onClick={() => selectAuthor(null)} aria-label="全部作品" title="全部作品"><SquaresFour size={21} /></button>
            <button className={libraryMode === 'authors' && !settingsActive ? 'active' : ''} onClick={focusAuthors} aria-label="作者" title="作者"><User size={22} /></button>
            <button onClick={onOpenRadar} aria-label="动态雷达" title="动态雷达"><Crosshair size={22} /></button>
          </nav>
          <div>
            <button className={settingsActive ? 'active' : ''} onClick={onOpenSettings} aria-label="系统设置" title="系统设置"><GearSix size={22} /></button>
            <span className="collapsed-source-status" title={`${sources.length} 个来源已启用`}><span className="status-dot" /></span>
          </div>
        </div>
      </aside>
    </>
  )
}
