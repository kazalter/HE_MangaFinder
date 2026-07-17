import type { WorkGroup } from '../types'
import { CoverImage } from './CoverImage'
import { statusMap, workAuthorLabel } from './WorkCard'

function displayDate(value: string | null) {
  return value
    ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }).format(new Date(value))
    : '更新时间未知'
}

export function WorkListRow({
  work,
  onOpen,
}: {
  work: WorkGroup
  onOpen: (work: WorkGroup) => void
}) {
  return (
    <button className="work-list-row" onClick={() => onOpen(work)}>
      <span className="list-cover">
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
        <small>{displayDate(work.latest_source_at)}</small>
      </span>
    </button>
  )
}
