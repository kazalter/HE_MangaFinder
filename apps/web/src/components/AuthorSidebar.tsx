import { useState, type FormEvent } from 'react'
import type { Author, Source } from '../types'

interface Props {
  authors: Author[]
  sources: Source[]
  selectedId: number | null
  busy: boolean
  onSelect: (id: number | null) => void
  onAdd: (name: string) => Promise<void>
  onRefresh: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
}

export function AuthorSidebar({
  authors,
  sources,
  selectedId,
  busy,
  onSelect,
  onAdd,
  onRefresh,
  onDelete,
}: Props) {
  const [name, setName] = useState('')

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return
    await onAdd(name)
    setName('')
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">漫</span>
        <div>
          <strong>MangaFinder</strong>
          <small>作者作品雷达</small>
        </div>
      </div>

      <form className="author-form" onSubmit={submit}>
        <label htmlFor="author-name">订阅一位作者</label>
        <div className="input-row">
          <input
            id="author-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：浅野一二〇"
            maxLength={200}
          />
          <button disabled={busy || !name.trim()} aria-label="添加作者">＋</button>
        </div>
        <p>会从已启用来源自动寻找作品</p>
      </form>

      <nav aria-label="作者订阅">
        <button
          className={`author-item ${selectedId === null ? 'active' : ''}`}
          onClick={() => onSelect(null)}
        >
          <span className="avatar all">全</span>
          <span className="author-copy"><b>全部作品</b><small>{authors.reduce((sum, item) => sum + item.work_count, 0)} 条关联</small></span>
        </button>
        {authors.map((author) => (
          <div className={`author-item-wrap ${selectedId === author.id ? 'active' : ''}`} key={author.id}>
            <button className="author-item" onClick={() => onSelect(author.id)}>
              <span className="avatar">{author.name.slice(0, 1).toUpperCase()}</span>
              <span className="author-copy"><b>{author.name}</b><small>{author.work_count} 部作品</small></span>
            </button>
            <div className="author-actions">
              <button title="立即刷新" onClick={() => onRefresh(author.id)}>↻</button>
              <button title="删除订阅" onClick={() => onDelete(author.id)}>×</button>
            </div>
          </div>
        ))}
      </nav>

      <div className="source-status">
        <span className="status-dot" />
        <div><b>{sources.length} 个来源已启用</b><small>{sources.map((source) => source.display_name).join(' · ') || '正在连接'}</small></div>
      </div>
    </aside>
  )
}
