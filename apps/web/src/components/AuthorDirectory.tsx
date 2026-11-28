import {
  ArrowClockwise,
  ArrowRight,
  BookOpenText,
  CheckCircle,
  Clock,
  MagnifyingGlass,
  Plus,
  Trash,
  WarningCircle,
} from '@phosphor-icons/react'
import { memo, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import type { Author, SocialStatus, WorkGroup } from '../types'
import { ExpandablePanel } from './ExpandablePanel'
import { AuthorXAccountsPanel, SocialRadar, type RadarFeedTab } from './SocialRadar'
import { ThemedSelect } from './ThemedSelect'

type AuthorSort = 'recent' | 'name' | 'works'
export type AuthorCenterTab = 'overview' | 'radar' | 'accounts'
const directoryCollator = new Intl.Collator('zh-CN')

function toTimestamp(value: string | null | undefined): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

const authorSortOptions: readonly { value: AuthorSort, label: string }[] = [
  { value: 'recent', label: '最近动态' },
  { value: 'name', label: '作者名称' },
  { value: 'works', label: '作品数量' },
]

interface Props {
  authors: Author[]
  works: WorkGroup[]
  socialStatus: SocialStatus | null
  previewAuthorId: number | null
  initialTab?: AuthorCenterTab
  initialRadarTab?: RadarFeedTab
  focusSignalId?: number | null
  busy?: boolean
  onPreviewAuthor: (id: number) => void
  onOpenAuthorWorks: (id: number) => void
  onOpenWork: (id: number) => void
  onAddAuthor?: (name: string, aliases?: string[]) => Promise<void>
  onRefreshAuthor?: (id: number) => Promise<void>
  onDeleteAuthor?: (id: number) => Promise<void>
  onChanged?: () => Promise<void>
  onGetQueryName?: (id: number, provider: string) => Promise<{ provider: string, query_name: string, origin: string, support: number }>
  onUpdateAuthor?: (
    id: number,
    identity: Pick<Author, 'name'> & Partial<Pick<Author, 'aliases' | 'circle_names'>>,
  ) => Promise<void>
}

function relativeTime(value: string | null): string {
  if (!value) return '尚未同步'
  const time = new Date(value).getTime()
  if (Number.isNaN(time)) return '时间未知'
  const minutes = Math.max(0, Math.round((Date.now() - time) / 60_000))
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.round(hours / 24)
  return days < 30 ? `${days} 天前` : new Date(value).toLocaleDateString('zh-CN')
}

const AuthorAvatar = memo(function AuthorAvatar({ author, large = false }: { author: Author, large?: boolean }) {
  return (
    <span className={`directory-avatar ${large ? 'large' : ''}`} aria-hidden="true">
      {author.avatar_url
        ? <img src={author.avatar_url} alt="" referrerPolicy="no-referrer" loading={large ? 'eager' : 'lazy'} decoding="async" />
        : author.name.slice(0, 1).toLocaleUpperCase()}
    </span>
  )
})

const SyncState = memo(function SyncState({ author }: { author: Author }) {
  if (author.x_sync_error) {
    return <span className="author-sync-state error"><WarningCircle size={15} />同步异常</span>
  }
  if (author.x_last_synced_at) {
    return <span className="author-sync-state ok"><CheckCircle size={15} />已同步</span>
  }
  if (author.x_handle) {
    return <span className="author-sync-state waiting"><Clock size={15} />等待同步</span>
  }
  return <span className="author-sync-state muted">未绑定 X</span>
})

export const AuthorDirectory = memo(function AuthorDirectory({
  authors,
  works,
  socialStatus,
  previewAuthorId,
  initialTab = 'overview',
  initialRadarTab = 'overview',
  focusSignalId = null,
  busy = false,
  onPreviewAuthor,
  onOpenAuthorWorks,
  onOpenWork,
  onAddAuthor,
  onRefreshAuthor,
  onDeleteAuthor,
  onChanged,
  onGetQueryName,
  onUpdateAuthor,
}: Props) {
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<AuthorSort>('recent')
  const [tab, setTab] = useState<AuthorCenterTab>(initialTab)
  const [addOpen, setAddOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newAliases, setNewAliases] = useState('')
  const inspectorRef = useRef<HTMLElement>(null)
  const [editingAliases, setEditingAliases] = useState(false)
  const [aliasesInput, setAliasesInput] = useState('')
  const [nameSuggestions, setNameSuggestions] = useState<{ provider: string, query_name: string, origin: string, support: number }[]>([])

  useEffect(() => {
    setTab(initialTab)
  }, [initialTab])

  async function submitNewAuthor(event: FormEvent) {
    event.preventDefault()
    if (!onAddAuthor || !newName.trim()) return
    const aliases = newAliases.split(/[,，、\n]+/).map((s) => s.trim()).filter(Boolean)
    try { await onAddAuthor(newName.trim(), aliases) }
    catch { return }
    setNewName(''); setNewAliases(''); setAddOpen(false)
  }

  async function saveAliases() {
    if (!previewAuthor || !onUpdateAuthor) return
    const nextAliases = aliasesInput
      .split(/[,，、\n]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    try { await onUpdateAuthor(previewAuthor.id, { name: previewAuthor.name, aliases: nextAliases }) }
    catch { return }
    setEditingAliases(false)
  }

  const indexedAuthors = useMemo(() => authors.map((author) => ({
    author,
    syncTime: toTimestamp(author.x_last_synced_at ?? author.last_checked_at),
    searchText: [
      author.name,
      ...(author.aliases ?? []),
      ...(author.circle_names ?? []),
      author.x_handle ?? '',
      author.x_display_name ?? '',
    ].join('\0').toLocaleLowerCase(),
  })), [authors])

  const filteredAuthors = useMemo(() => {
    const term = query.trim().toLocaleLowerCase()
    const filtered = term
      ? indexedAuthors.filter((entry) => entry.searchText.includes(term))
      : [...indexedAuthors]
    filtered.sort((left, right) => {
      if (sort === 'name') return directoryCollator.compare(left.author.name, right.author.name)
      if (sort === 'works') return right.author.work_count - left.author.work_count || directoryCollator.compare(left.author.name, right.author.name)
      return right.syncTime - left.syncTime
    })
    return filtered.map((entry) => entry.author)
  }, [indexedAuthors, query, sort])

  const previewAuthor = authors.find((author) => author.id === previewAuthorId)
    ?? filteredAuthors[0]
    ?? authors[0]

  useEffect(() => {
    let active = true
    setNameSuggestions([])
    if (!previewAuthor || !onGetQueryName) return () => { active = false }
    void Promise.allSettled(['wnacg', 'nhentai'].map((provider) => onGetQueryName(previewAuthor.id, provider)))
      .then((results) => {
        if (!active) return
        const known = [
          previewAuthor.name,
          ...(previewAuthor.aliases ?? []),
          ...(previewAuthor.circle_names ?? []),
          ...Object.values(previewAuthor.provider_query_names ?? {}),
        ].map((name) => name.normalize('NFKC').toLocaleLowerCase())
        setNameSuggestions(results.flatMap((result) => result.status === 'fulfilled'
          && result.value.origin === 'inferred'
          && !known.includes(result.value.query_name.normalize('NFKC').toLocaleLowerCase()) ? [result.value] : []))
      })
    return () => { active = false }
  }, [previewAuthor, onGetQueryName])

  async function confirmNameSuggestion(name: string) {
    if (!previewAuthor || !onUpdateAuthor) return
    try { await onUpdateAuthor(previewAuthor.id, { name: previewAuthor.name, aliases: [...(previewAuthor.aliases ?? []), name] }) }
    catch { return }
  }

  const previewWorks = useMemo(() => {
    if (!previewAuthor) return []
    return works
      .filter((work) => work.authors.some((author) => author.id === previewAuthor.id))
      .map((work) => ({ work, time: toTimestamp(work.latest_source_at ?? work.first_source_at) }))
      .sort((left, right) => right.time - left.time)
      .slice(0, 6)
      .map((entry) => entry.work)
  }, [previewAuthor, works])

  return (
    <section className="author-directory" aria-label="作者中心">
      <section className="author-index-pane">
        <header className="author-directory-header">
          <div>
            <p className="eyebrow">LIBRARY / AUTHORS</p>
            <h1>作者中心</h1>
            <p>一站式管理已订阅作者、查看代表作并追踪 X 动态与情报。</p>
          </div>
          {onAddAuthor && (
            <button
              className="author-add-trigger"
              onClick={() => setAddOpen((current) => !current)}
              aria-expanded={addOpen}
            >
              <Plus size={16} />{addOpen ? '收起' : '订阅作者'}
            </button>
          )}
        </header>

        {onAddAuthor && (
          <ExpandablePanel open={addOpen} className="directory-add-panel">
            <form className="directory-add-form" onSubmit={(event) => void submitNewAuthor(event)}>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="作者名称（必填）" maxLength={200} />
              <input value={newAliases} onChange={(e) => setNewAliases(e.target.value)} placeholder="别名 / 备选名（逗号分隔，可选）" maxLength={500} />
              <button disabled={busy || !newName.trim()}>确认订阅</button>
            </form>
          </ExpandablePanel>
        )}

        <div className="author-directory-controls">
          <label className="author-directory-search">
            <MagnifyingGlass size={19} />
            <span className="sr-only">搜索作者中心</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索作者名称、别名或 @handle"
            />
          </label>
          <ThemedSelect label="排序" value={sort} options={authorSortOptions} onChange={setSort} />
        </div>

        <div className="author-index-head" aria-hidden="true">
          <span>作者</span><span>作品数</span><span>X 状态</span><span>最近动态</span>
        </div>
        <div className="author-index-list" role="listbox" aria-label="作者列表">
          {filteredAuthors.map((author) => {
            const active = author.id === previewAuthor?.id
            return (
              <button
                className={`author-index-row ${active ? 'active' : ''}`}
                key={author.id}
                role="option"
                aria-selected={active}
                onClick={() => {
                  setEditingAliases(false)
                  onPreviewAuthor(author.id)
                  if (window.matchMedia?.('(max-width: 800px)').matches) {
                    inspectorRef.current?.scrollIntoView({ behavior: 'auto', block: 'start' })
                  }
                }}
              >
                <span className="author-index-identity">
                  <AuthorAvatar author={author} />
                  <span><b>{author.name}</b><small>{author.x_handle ? `@${author.x_handle}` : '未绑定 X'}</small></span>
                </span>
                <span className="author-index-count">{author.work_count}</span>
                <SyncState author={author} />
                <time dateTime={author.x_last_synced_at ?? author.last_checked_at ?? undefined}>
                  {relativeTime(author.x_last_synced_at ?? author.last_checked_at)}
                </time>
              </button>
            )
          })}
          {!filteredAuthors.length && <div className="author-directory-empty">没有匹配的作者</div>}
        </div>
      </section>

      <aside ref={inspectorRef} className="author-inspector" aria-live="polite">
        {previewAuthor ? <>
          <div className="author-inspector-profile">
            <AuthorAvatar author={previewAuthor} large />
            <div>
              <h2>{previewAuthor.name}</h2>
              {previewAuthor.x_handle
                ? <a href={`https://x.com/${previewAuthor.x_handle}`} target="_blank" rel="noreferrer">@{previewAuthor.x_handle}</a>
                : <span>未绑定 X 账号</span>}
              {previewAuthor.x_display_name && previewAuthor.x_display_name !== previewAuthor.name
                ? <small>{previewAuthor.x_display_name}</small>
                : null}
              {editingAliases ? (
                <div className="author-aliases-edit-row">
                  <input
                    value={aliasesInput}
                    onChange={(e) => setAliasesInput(e.target.value)}
                    placeholder="别名 / 备选名（逗号分隔）"
                    className="author-aliases-input"
                  />
                  <div className="author-aliases-edit-actions">
                    <button type="button" onClick={() => void saveAliases()} className="author-aliases-save-btn">保存</button>
                    <button type="button" onClick={() => setEditingAliases(false)} className="author-aliases-cancel-btn">取消</button>
                  </div>
                </div>
              ) : (
                <div className="author-aliases-display-row">
                  <small>别名：{previewAuthor.aliases?.length ? previewAuthor.aliases.join(' · ') : '暂无'}</small>
                  {onUpdateAuthor && (
                    <button
                      type="button"
                      onClick={() => {
                        setEditingAliases(true)
                        setAliasesInput(previewAuthor.aliases?.join(', ') ?? '')
                      }}
                      className="author-aliases-edit-toggle"
                    >
                      编辑别名
                    </button>
                  )}
                </div>
              )}
              {previewAuthor.circle_names?.length ? <small>社团：{previewAuthor.circle_names.join(' · ')}</small> : null}
            </div>
          </div>

          {nameSuggestions.length > 0 && (
            <div className="author-name-suggestions">
              <strong>待核对的来源名称</strong>
              <small>以下名称由历史作品推测，可能是合作者；确认前不会作为作者别名自动收录。</small>
              {nameSuggestions.map((item) => (
                <div key={`${item.provider}:${item.query_name}`}>
                  <span>{item.provider}：{item.query_name}{item.support > 0 ? `（${item.support} 条作品）` : ''}</span>
                  {onUpdateAuthor && <button type="button" onClick={() => void confirmNameSuggestion(item.query_name)}>确认是别名</button>}
                </div>
              ))}
            </div>
          )}

          <div className="author-hero-actions">
            <button className="author-open-button" onClick={() => onOpenAuthorWorks(previewAuthor.id)}>
              查看 {previewAuthor.work_count} 部作品 <ArrowRight size={17} />
            </button>
            {onRefreshAuthor && (
              <button className="author-secondary-btn" disabled={busy} onClick={() => void onRefreshAuthor(previewAuthor.id)}>
                <ArrowClockwise size={16} />刷新作品
              </button>
            )}
          </div>

          <nav className="author-center-tabs" aria-label="作者详情分区">
            <button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>作品与概况</button>
            <button className={tab === 'radar' ? 'active' : ''} onClick={() => setTab('radar')}>动态与情报</button>
            <button className={tab === 'accounts' ? 'active' : ''} onClick={() => setTab('accounts')}>账号与设置</button>
          </nav>

          {tab === 'overview' && <>
            <section className="author-inspector-section">
              <header>
                <h3>代表作品</h3>
                <button onClick={() => onOpenAuthorWorks(previewAuthor.id)}>全部作品 <ArrowRight size={13} /></button>
              </header>
              <div className="author-preview-covers">
                {previewWorks.map((work) => (
                  <button type="button" className="author-preview-tile" key={work.id} onClick={() => onOpenWork(work.id)}>
                    <div>{work.cover_url ? <img src={work.cover_url} alt="" loading="lazy" decoding="async" /> : <BookOpenText size={24} />}</div>
                    <b>{work.title}</b>
                  </button>
                ))}
                {!previewWorks.length && <p>还没有可展示的作品封面。</p>}
              </div>
            </section>

            <section className="author-inspector-section author-inspector-sync">
              <header>
                <h3>动态同步</h3>
                <button onClick={() => setTab(previewAuthor.x_handle ? 'radar' : 'accounts')}>
                  {previewAuthor.x_handle ? '进入动态中心' : '绑定 X 账号'} <ArrowRight size={13} />
                </button>
              </header>
              <SyncState author={previewAuthor} />
              <p>{previewAuthor.x_sync_error || (previewAuthor.x_last_synced_at
                ? `最近同步于 ${relativeTime(previewAuthor.x_last_synced_at)}`
                : '绑定 X 账号后可自动同步作者动态。')}</p>
            </section>
          </>}

          {tab === 'radar' && socialStatus && (
            <SocialRadar
              author={previewAuthor}
              status={socialStatus}
              works={works}
              busy={busy}
              focusSignalId={focusSignalId}
              initialFeedTab={initialRadarTab}
              onChanged={onChanged ?? (async () => undefined)}
              onOpenWork={onOpenWork}
              onOpenAccountSetup={() => setTab('accounts')}
            />
          )}

          {tab === 'accounts' && (
            <div className="author-accounts-tab">
              {socialStatus && (
                <AuthorXAccountsPanel
                  author={previewAuthor}
                  status={socialStatus}
                  busy={busy}
                  onChanged={onChanged ?? (async () => undefined)}
                />
              )}
              {onDeleteAuthor && (
                <section className="author-danger-zone">
                  <div>
                    <strong>取消订阅作者</strong>
                    <small>删除后不会再追踪该作者新作品；已发现的历史作品资料仍会保留。</small>
                  </div>
                  <button className="danger-btn" disabled={busy} onClick={() => void onDeleteAuthor(previewAuthor.id)}>
                    <Trash size={15} />删除订阅
                  </button>
                </section>
              )}
            </div>
          )}
        </> : <div className="author-directory-empty">还没有订阅作者</div>}
      </aside>
    </section>
  )
})
