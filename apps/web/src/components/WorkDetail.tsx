import { useMemo, useState } from 'react'
import type { Edition, WorkGroup, WorkGroupDetail, WorkSource } from '../types'
import { useAnimatedClose } from '../hooks/useAnimatedClose'
import { CoverImage } from './CoverImage'

interface Props {
  group: WorkGroupDetail
  allGroups: WorkGroup[]
  busy: boolean
  enabledProviders: string[]
  activeAuthorId: number | null
  onClose: () => void
  onDownload: (edition: Edition, source: WorkSource) => void
  onSplit: (workId: number) => void
  onMerge: (targetGroupId: number) => void
  onExcludeEdition: (authorId: number, workId: number) => void
  onExcludeGroup: (authorId: number) => void
}

function displayDate(value: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value)) : '日期未知'
}

export function WorkDetail({ group, allGroups, busy, enabledProviders, activeAuthorId, onClose, onDownload, onSplit, onMerge, onExcludeEdition, onExcludeGroup }: Props) {
  const { closing, requestClose } = useAnimatedClose(onClose)
  const [provider, setProvider] = useState('all')
  const [mergeTarget, setMergeTarget] = useState('')
  const [exclusionAuthor, setExclusionAuthor] = useState(() => {
    if (activeAuthorId && group.authors.some((author) => author.id === activeAuthorId)) return String(activeAuthorId)
    return group.authors.length === 1 ? String(group.authors[0].id) : ''
  })
  const providers = group.providers
  const editions = useMemo(() => group.editions.filter((edition) => (
    provider === 'all' || edition.sources.some((source) => source.provider === provider)
  )), [group.editions, provider])

  return (
    <div className={`modal-backdrop work-detail-backdrop${closing ? ' is-closing' : ''}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }}>
      <section className="work-detail" role="dialog" aria-modal="true" aria-label={`${group.title} 版本详情`}>
        <button className="modal-close" onClick={requestClose} aria-label="关闭">×</button>
        <header className="detail-header">
          <CoverImage src={group.cover_url} alt={`${group.title} 封面`} fallback={<div className="detail-cover-empty">冊</div>} />
          <div>
            <p className="eyebrow">WORK / EDITIONS</p>
            <h2>{group.title}</h2>
            <p>{group.description?.replace(/\[[^\]]+\]\([^\)]+\)/g, '') || '暂无简介'}</p>
            <div className="source-badges large">
              {providers.map((item) => <span className={`source-badge source-${item}`} key={item}>{item}</span>)}
              <small>{group.edition_count} 个版本 · 作品时间 {displayDate(group.first_source_at)} · 最近版本 {displayDate(group.latest_source_at)}</small>
            </div>
          </div>
        </header>

        {(group.series || group.relations?.length) && <section className="exclusion-panel series-panel">
          <div>
            <strong>{group.series ? `系列 · ${group.series.title}` : '作品关系'}</strong>
            <small>
              {group.series?.volume_number ? `第 ${group.series.volume_number} 卷` : group.series?.label || '未指定卷号'}
              {group.series?.missing_volumes.length ? ` · 可能缺少第 ${group.series.missing_volumes.join('、')} 卷` : ''}
            </small>
          </div>
          <div className="variant-labels">
            {group.relations?.map((relation) => <span key={relation.id}>{({ prequel: '前作', sequel: '续作', side_story: '番外', compilation: '总集篇' } as const)[relation.relation_type]} · {relation.related_title}</span>)}
          </div>
        </section>}

        <div className="provider-filter" aria-label="按来源筛选">
          {['all', ...providers].map((item) => (
            <button className={provider === item ? 'active' : ''} onClick={() => setProvider(item)} key={item}>
              {item === 'all' ? `全部 ${group.edition_count}` : item}
            </button>
          ))}
        </div>

        <div className="edition-list">
          {editions.map((edition) => (
            <article className="edition-card" key={edition.work_id}>
              <CoverImage src={edition.cover_url} alt={`${edition.title} 封面`} loading="lazy" fallback={<div className="edition-cover-empty">冊</div>} />
              <div className="edition-copy">
                <h3>{edition.title}</h3>
                <p>{displayDate(edition.latest_source_at)}{edition.language ? ` · ${edition.language.toUpperCase()}` : ''}</p>
                <div className="variant-labels">
                  {edition.variant_labels.length ? edition.variant_labels.map((label) => <span key={label}>{label}</span>) : <span>普通版</span>}
                </div>
                <div className="edition-sources">
                  {edition.sources.filter((source) => provider === 'all' || source.provider === provider).map((source) => (
                    <div key={`${source.provider}-${source.external_id}`}>
                      <span className={`source-badge source-${source.provider}`}>{source.provider}</span>
                      <a href={source.source_url} target="_blank" rel="noreferrer">原站 ↗</a>
                      <button disabled={busy || !enabledProviders.includes(source.provider)} onClick={() => onDownload(edition, source)}>{enabledProviders.includes(source.provider) ? '选择章节并下载 ↓' : '历史来源（已停用）'}</button>
                    </div>
                  ))}
                </div>
                <div className="edition-correction-actions">
                  {group.edition_count > 1 && <button className="split-link" disabled={busy} onClick={() => onSplit(edition.work_id)}>聚合有误？拆分此版本</button>}
                  {exclusionAuthor && edition.authors.some((author) => author.id === Number(exclusionAuthor)) && <button className="exclude-link" disabled={busy} onClick={() => onExcludeEdition(Number(exclusionAuthor), edition.work_id)}>不是该作者的版本</button>}
                </div>
              </div>
            </article>
          ))}
        </div>

        {group.authors.length > 0 && <section className="exclusion-panel">
          <div><strong>纠正误收录</strong><small>移除后会记入排除名单，今后刷新不会再次自动添加同一来源。</small></div>
          <select value={exclusionAuthor} onChange={(event) => setExclusionAuthor(event.target.value)} aria-label="选择要纠正的作者">
            <option value="">选择作者…</option>
            {group.authors.map((author) => <option value={author.id} key={author.id}>{author.name}</option>)}
          </select>
          <button className="danger" disabled={!exclusionAuthor || busy} onClick={() => onExcludeGroup(Number(exclusionAuthor))}>整部都不是该作者作品</button>
        </section>}

        <footer className="manual-merge">
          <div><strong>人工合并</strong><small>自动算法漏掉时，可把当前作品并入另一部作品；此操作会被保护。</small></div>
          <select value={mergeTarget} onChange={(event) => setMergeTarget(event.target.value)}>
            <option value="">选择目标作品…</option>
            {allGroups.filter((item) => item.id !== group.id).map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}
          </select>
          <button disabled={!mergeTarget || busy} onClick={() => onMerge(Number(mergeTarget))}>确认合并</button>
        </footer>
      </section>
    </div>
  )
}
