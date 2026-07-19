import {
  ArrowRight,
  BookOpenText,
  CheckCircle,
  Clock,
  MagnifyingGlass,
  WarningCircle,
} from '@phosphor-icons/react'
import { useMemo, useState } from 'react'
import type { Author, WorkGroup } from '../types'

type AuthorSort = 'recent' | 'name' | 'works'

interface Props {
  authors: Author[]
  works: WorkGroup[]
  previewAuthorId: number | null
  onPreviewAuthor: (id: number) => void
  onOpenAuthor: (id: number) => void
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

function AuthorAvatar({ author, large = false }: { author: Author, large?: boolean }) {
  return (
    <span className={`directory-avatar ${large ? 'large' : ''}`} aria-hidden="true">
      {author.avatar_url
        ? <img src={author.avatar_url} alt="" referrerPolicy="no-referrer" />
        : author.name.slice(0, 1).toLocaleUpperCase()}
    </span>
  )
}

function SyncState({ author }: { author: Author }) {
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
}

export function AuthorDirectory({
  authors,
  works,
  previewAuthorId,
  onPreviewAuthor,
  onOpenAuthor,
}: Props) {
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<AuthorSort>('recent')
  const filteredAuthors = useMemo(() => {
    const term = query.trim().toLocaleLowerCase()
    const filtered = authors.filter((author) => (
      !term
      || author.name.toLocaleLowerCase().includes(term)
      || author.x_handle?.toLocaleLowerCase().includes(term)
      || author.x_display_name?.toLocaleLowerCase().includes(term)
    ))
    return [...filtered].sort((left, right) => {
      if (sort === 'name') return left.name.localeCompare(right.name, 'zh-CN')
      if (sort === 'works') return right.work_count - left.work_count || left.name.localeCompare(right.name, 'zh-CN')
      return new Date(right.x_last_synced_at ?? right.last_checked_at ?? 0).getTime()
        - new Date(left.x_last_synced_at ?? left.last_checked_at ?? 0).getTime()
    })
  }, [authors, query, sort])
  const previewAuthor = authors.find((author) => author.id === previewAuthorId)
    ?? filteredAuthors[0]
    ?? authors[0]
  const previewWorks = useMemo(() => {
    if (!previewAuthor) return []
    return works
      .filter((work) => work.authors.some((author) => author.id === previewAuthor.id))
      .sort((left, right) => (
        new Date(right.latest_source_at ?? right.first_source_at ?? 0).getTime()
        - new Date(left.latest_source_at ?? left.first_source_at ?? 0).getTime()
      ))
      .slice(0, 3)
  }, [previewAuthor, works])

  return (
    <section className="author-directory" aria-label="作者目录">
      <section className="author-index-pane">
        <header className="author-directory-header">
          <p className="eyebrow">LIBRARY / AUTHORS</p>
          <h1>作者目录</h1>
          <p>浏览已订阅的作者，选择一位查看作品与同步状态。</p>
        </header>

          <div className="author-directory-controls">
            <label className="author-directory-search">
              <MagnifyingGlass size={19} />
              <span className="sr-only">搜索作者目录</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索作者名称或 @handle"
              />
            </label>
            <label className="author-directory-sort">
              <span>排序</span>
              <select value={sort} onChange={(event) => setSort(event.target.value as AuthorSort)}>
                <option value="recent">最近动态</option>
                <option value="name">作者名称</option>
                <option value="works">作品数量</option>
              </select>
            </label>
          </div>

          <div className="author-index-head" aria-hidden="true">
            <span>作者</span><span>作品数</span><span>同步状态</span><span>最近动态</span>
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
                  onClick={() => onPreviewAuthor(author.id)}
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

      <aside className="author-inspector" aria-live="polite">
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
              </div>
            </div>
            <button className="author-open-button" onClick={() => onOpenAuthor(previewAuthor.id)}>
              查看 {previewAuthor.work_count} 部作品 <ArrowRight size={17} />
            </button>

            <section className="author-inspector-section">
              <header><h3>代表作品</h3><button onClick={() => onOpenAuthor(previewAuthor.id)}>全部作品 <ArrowRight size={13} /></button></header>
              <div className="author-preview-covers">
                {previewWorks.map((work) => <article key={work.id}>
                  <div>{work.cover_url ? <img src={work.cover_url} alt="" loading="lazy" /> : <BookOpenText size={24} />}</div>
                  <b>{work.title}</b>
                </article>)}
                {!previewWorks.length && <p>还没有可展示的作品封面。</p>}
              </div>
            </section>

            <section className="author-inspector-section author-inspector-sync">
              <h3>动态同步</h3>
              <SyncState author={previewAuthor} />
              <p>{previewAuthor.x_sync_error || (previewAuthor.x_last_synced_at
                ? `最近同步于 ${relativeTime(previewAuthor.x_last_synced_at)}`
                : '绑定 X 账号后可自动同步作者动态。')}</p>
            </section>
          </> : <div className="author-directory-empty">还没有订阅作者</div>}
      </aside>
    </section>
  )
}
