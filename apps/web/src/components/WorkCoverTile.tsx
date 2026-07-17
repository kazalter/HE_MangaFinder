import type { WorkGroup } from '../types'
import { CoverImage } from './CoverImage'
import { statusMap, workAuthorLabel } from './WorkCard'

export function WorkCoverTile({
  work,
  onOpen,
}: {
  work: WorkGroup
  onOpen: (work: WorkGroup) => void
}) {
  return (
    <button
      className="work-cover-tile"
      onClick={() => onOpen(work)}
      aria-label={`查看《${work.title}》`}
    >
      <span className="cover-tile-image">
        <CoverImage
          src={work.cover_url}
          alt={`${work.title} 封面`}
          loading="lazy"
          fallback={<span className="cover-placeholder">暂无封面</span>}
        />
        {work.status && <span className="status-pill">{statusMap[work.status] ?? work.status}</span>}
      </span>
      <span className="cover-tile-copy">
        <small>{workAuthorLabel(work)}</small>
        <strong>{work.title}</strong>
        <span>{[work.year, `${work.edition_count} 个版本`].filter(Boolean).join(' · ')}</span>
      </span>
    </button>
  )
}
