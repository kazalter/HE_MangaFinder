import type { WorkGroup } from '../types'
import { CoverImage } from './CoverImage'

export const statusMap: Record<string, string> = {
  ongoing: '连载中', completed: '已完结', hiatus: '暂停', cancelled: '已取消',
}

export function workAuthorLabel(work: WorkGroup) {
  return work.authors.map((author) => author.name).join('、') || '未关联作者'
}

export function WorkCard({ work, onOpen }: { work: WorkGroup, onOpen: (work: WorkGroup) => void }) {
  return (
    <article
      className="work-card"
      onClick={() => onOpen(work)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen(work)
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="cover-wrap">
        <CoverImage
          src={work.cover_url}
          alt={`${work.title} 封面`}
          loading="lazy"
          fallback={<div className="cover-placeholder">{work.cover_url ? '封面加载失败' : '暂无封面'}</div>}
        />
        {work.status && <span className="status-pill">{statusMap[work.status] ?? work.status}</span>}
      </div>
      <div className="work-info">
        <p className="work-author">{workAuthorLabel(work)}</p>
        <h3>{work.title}</h3>
        <p className="metadata">{[work.year, work.language?.toUpperCase()].filter(Boolean).join(' · ') || '年份未知'}</p>
        {work.description && <p className="description">{work.description.replace(/\[[^\]]+\]\([^\)]+\)/g, '')}</p>}
        <div className="tags">
          {work.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}
        </div>
        <div className="source-badges">
          {work.providers.map((provider) => <span className={`source-badge source-${provider}`} key={provider}>{provider}</span>)}
        </div>
        <div className="card-actions">
          <span>{work.edition_count} 个版本</span>
          <button onClick={(event) => { event.stopPropagation(); onOpen(work) }}>查看版本 →</button>
        </div>
      </div>
    </article>
  )
}
