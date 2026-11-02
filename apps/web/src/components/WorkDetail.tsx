import { useMemo, useState, type FormEvent } from 'react'
import type { Edition, WorkGroup, WorkGroupDetail, WorkMetadataPatch, WorkSource } from '../types'
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
  onUpdateMetadata: (patch: WorkMetadataPatch) => Promise<boolean>
  onSplit: (workId: number) => void
  onMerge: (targetGroupId: number) => void
  onExcludeEdition: (authorId: number, workId: number) => void
  onExcludeGroup: (authorId: number) => void
}

function displayDate(value: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value)) : '日期未知'
}

export function WorkDetail({ group, allGroups, busy, enabledProviders, activeAuthorId, onClose, onDownload, onUpdateMetadata, onSplit, onMerge, onExcludeEdition, onExcludeGroup }: Props) {
  const { closing, requestClose } = useAnimatedClose(onClose)
  const [provider, setProvider] = useState('all')
  const [mergeTarget, setMergeTarget] = useState('')
  const [editingMetadata, setEditingMetadata] = useState(false)
  const [metadataError, setMetadataError] = useState('')
  const overrides = group.metadata_overrides ?? {}
  async function saveMetadata(patch: WorkMetadataPatch): Promise<boolean> {
    setMetadataError('')
    const ok = await onUpdateMetadata(patch)
    if (!ok) setMetadataError('保存失败，请检查输入或登录状态后重试。')
    return ok
  }
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

        <section className="metadata-panel">
          <header><div><strong>作品资料</strong><small>查看字段来源，手工固定后会保留你的选择。</small></div><button onClick={() => setEditingMetadata((value) => !value)}>{editingMetadata ? '收起' : '编辑资料'}</button></header>
          {group.metadata_sources && <div className="metadata-sources">
            {([['title', '标题'], ['cover_url', '封面'], ['year', '年份'], ['description', '简介'], ['status', '状态'], ['language', '语言'], ['tags', '标签']] as const).map(([key, label]) => {
              const source = group.metadata_sources?.[key]
              if (!source) return null
              const origin = source.edition_ids.length ? `版本 ${source.edition_ids.join('、')} · ${source.providers.join('、') || '未知来源'}` : source.locked ? '手工设置' : '暂无来源'
              return <span key={key}>{label}：{source.locked ? '固定 · ' : ''}{origin}</span>
            })}
          </div>}
          {editingMetadata && <MetadataEditor group={group} busy={busy} onSave={async (patch) => { if (await saveMetadata(patch)) setEditingMetadata(false) }} />}
          {overrides.cover_work_id && !group.editions.some((edition) => edition.work_id === overrides.cover_work_id && edition.cover_url) && <p>固定的封面暂不可用。<button disabled={busy} onClick={() => void saveMetadata({ cover_work_id: null })}>解除封面固定</button></p>}
          {metadataError && <p className="metadata-error" role="alert">{metadataError}</p>}
        </section>

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
                <p>来源更新 {displayDate(edition.latest_source_at)}{edition.language ? ` · ${edition.language.toUpperCase()}` : ''}{edition.year ? ` · 发行年 ${edition.year}` : ''}{edition.page_count ? ` · ${edition.page_count} 页` : ''}</p>
                {edition.tags.length > 0 && <p>标签：{edition.tags.join('、')}</p>}
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
                  <button disabled={busy} onClick={() => void saveMetadata({ preferred_work_id: overrides.preferred_work_id === edition.work_id ? null : edition.work_id })}>{overrides.preferred_work_id === edition.work_id ? '已设为首选 · 恢复自动' : '设为首选版本'}</button>
                  {edition.cover_url && <button disabled={busy} onClick={() => void saveMetadata({ cover_work_id: overrides.cover_work_id === edition.work_id ? null : edition.work_id })}>{overrides.cover_work_id === edition.work_id ? '已固定封面 · 恢复自动' : '固定此封面'}</button>}
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


function MetadataEditor({ group, busy, onSave }: { group: WorkGroupDetail, busy: boolean, onSave: (patch: WorkMetadataPatch) => Promise<void> }) {
  const overrides = group.metadata_overrides ?? {}
  const [title, setTitle] = useState(group.title)
  const [description, setDescription] = useState(group.description ?? '')
  const [year, setYear] = useState(group.year?.toString() ?? '')
  const [language, setLanguage] = useState(group.language ?? '')
  const [tags, setTags] = useState(group.tags.join('，'))
  const allowedStatus = ['ongoing', 'completed', 'hiatus', 'cancelled'] as const
  const [status, setStatus] = useState<(typeof allowedStatus)[number]>(allowedStatus.find((item) => item === group.status) ?? 'ongoing')
  const [locked, setLocked] = useState(() => ({
    title: 'title' in overrides, description: 'description' in overrides, year: 'year' in overrides,
    status: 'status' in overrides, language: 'language' in overrides, tags: 'tags' in overrides,
  }))
  const toggle = (key: keyof typeof locked) => setLocked((current) => ({ ...current, [key]: !current[key] }))
  function submit(event: FormEvent) {
    event.preventDefault()
    void onSave({
      title: locked.title ? title.trim() : null, description: locked.description ? description : null,
      year: locked.year ? Number(year) : null, status: locked.status ? status : null,
      language: locked.language ? language.trim() : null,
      tags: locked.tags ? tags.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean) : null,
    })
  }
  return <form className="metadata-editor" onSubmit={submit}>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.title} onChange={() => toggle('title')} /> 固定标题</label><input aria-label="标题" value={title} disabled={!locked.title} required={locked.title} maxLength={500} onChange={(e) => setTitle(e.target.value)} /></div>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.year} onChange={() => toggle('year')} /> 固定发行年</label><input aria-label="发行年" type="number" min="1" max="3000" value={year} disabled={!locked.year} required={locked.year} onChange={(e) => setYear(e.target.value)} /></div>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.description} onChange={() => toggle('description')} /> 固定简介</label><textarea aria-label="简介" value={description} disabled={!locked.description} maxLength={50000} onChange={(e) => setDescription(e.target.value)} /></div>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.status} onChange={() => toggle('status')} /> 固定状态</label><select aria-label="状态" value={status} disabled={!locked.status} onChange={(e) => setStatus(e.target.value as typeof status)}><option value="ongoing">连载中</option><option value="completed">已完结</option><option value="hiatus">暂停</option><option value="cancelled">已取消</option></select></div>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.language} onChange={() => toggle('language')} /> 固定语言</label><input aria-label="语言" value={language} disabled={!locked.language} required={locked.language} maxLength={20} onChange={(e) => setLanguage(e.target.value)} /></div>
    <div className="metadata-field"><label><input type="checkbox" checked={locked.tags} onChange={() => toggle('tags')} /> 固定标签</label><input aria-label="标签" value={tags} disabled={!locked.tags} onChange={(e) => setTags(e.target.value)} placeholder="用逗号分隔" /></div>
    <p>取消勾选会恢复自动选择；封面和首选版本可在下方版本列表设置。</p>
    <button type="submit" disabled={busy}>保存资料</button>
  </form>
}
