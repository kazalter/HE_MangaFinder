import { useCallback, useEffect, useMemo, useState } from 'react'
import { AuthorSidebar } from './components/AuthorSidebar'
import { MergeReview } from './components/MergeReview'
import { SocialRadar } from './components/SocialRadar'
import { WorkCard } from './components/WorkCard'
import { WorkDetail } from './components/WorkDetail'
import { api } from './lib/api'
import type { AgentStatus, Author, Edition, Job, MergeSuggestion, ReleaseSignal, SocialStatus, Source, WorkGroup, WorkGroupDetail, WorkSource } from './types'

export default function App() {
  const [authors, setAuthors] = useState<Author[]>([])
  const [works, setWorks] = useState<WorkGroup[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([])
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null)
  const [openedWork, setOpenedWork] = useState<WorkGroupDetail | null>(null)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [radarOpen, setRadarOpen] = useState(() => new URLSearchParams(window.location.search).has('radar'))
  const [focusSignalId] = useState<number | null>(() => {
    const value = new URLSearchParams(window.location.search).get('radar')
    return value && /^\d+$/.test(value) ? Number(value) : null
  })
  const [socialStatus, setSocialStatus] = useState<SocialStatus | null>(null)
  const [socialSignals, setSocialSignals] = useState<ReleaseSignal[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  const load = useCallback(async () => {
    try {
      const [nextAuthors, nextWorks, nextJobs, nextSources, nextSuggestions, nextAgentStatus, nextSocialStatus, nextSocialSignals] = await Promise.all([
        api.authors(), api.workGroups(selectedId ?? undefined), api.jobs(), api.sources(), api.mergeSuggestions(), api.agentStatus(), api.socialStatus(), api.socialRadar(selectedId ?? undefined),
      ])
      setAuthors(nextAuthors)
      setWorks(nextWorks)
      setJobs(nextJobs)
      setSources(nextSources)
      setSuggestions(nextSuggestions)
      setAgentStatus(nextAgentStatus)
      setSocialStatus(nextSocialStatus)
      setSocialSignals(nextSocialSignals)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '加载失败')
    }
  }, [selectedId])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!jobs.some((job) => job.status === 'pending' || job.status === 'running')) return
    const timer = window.setInterval(() => void load(), 2000)
    return () => window.clearInterval(timer)
  }, [jobs, load])

  const selectedAuthor = authors.find((author) => author.id === selectedId)
  const visibleWorks = useMemo(() => {
    const term = search.trim().toLocaleLowerCase()
    return term ? works.filter((work) => work.title.toLocaleLowerCase().includes(term)) : works
  }, [search, works])
  const activeJobs = jobs.filter((job) => job.status === 'pending' || job.status === 'running')
  const failedJob = jobs.find((job) => job.status === 'failed')

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try { await action(); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败') }
    finally { setBusy(false) }
  }

  async function openWork(work: WorkGroup | number) {
    setBusy(true)
    try { setOpenedWork(await api.workGroup(typeof work === 'number' ? work : work.id)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '加载版本失败') }
    finally { setBusy(false) }
  }

  async function downloadEdition(edition: Edition, source: WorkSource) {
    setBusy(true)
    setError(null)
    try {
      const chapters = await api.chapters(edition.work_id, source.provider)
      if (!chapters.length) throw new Error('这个来源暂时没有可下载章节')
      const chapter = chapters[0]
      const label = chapter.number ? `第 ${chapter.number} 话` : (chapter.title ?? '最新章节')
      if (!window.confirm(`从 ${source.provider} 把《${edition.title}》${label}加入 CBZ 下载队列？`)) return
      await api.downloadChapter(edition.work_id, source.provider, chapter.external_id)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建下载任务失败')
    } finally {
      setBusy(false)
    }
  }

  async function splitEdition(workId: number) {
    if (!openedWork || !window.confirm('确认把这个版本拆成一部独立作品？')) return
    await act(() => api.splitEdition(openedWork.id, workId))
    setOpenedWork(null)
  }

  async function mergeCurrentInto(targetGroupId: number) {
    if (!openedWork || !window.confirm('确认合并？所有版本都会保留，此关系会标记为人工决定。')) return
    await act(() => api.mergeGroups(targetGroupId, openedWork.id))
    setOpenedWork(null)
  }

  async function reviewSuggestion(id: number, accept: boolean) {
    await act(() => accept ? api.acceptSuggestion(id) : api.rejectSuggestion(id))
  }

  return (
    <div className="shell">
      <AuthorSidebar
        authors={authors}
        sources={sources}
        selectedId={selectedId}
        busy={busy}
        onSelect={setSelectedId}
        onAdd={(name) => act(() => api.createAuthor(name))}
        onRefresh={(id) => act(() => api.refreshAuthor(id))}
        onDelete={async (id) => {
          if (!window.confirm('删除这个作者订阅？已发现的作品资料会保留。')) return
          if (selectedId === id) setSelectedId(null)
          await act(() => api.deleteAuthor(id))
        }}
      />

      <main>
        <header className="topbar">
          <div>
            <p className="eyebrow">LIBRARY / DISCOVERY</p>
            <h1>{selectedAuthor?.name ?? '作品总览'}</h1>
            <p>{selectedAuthor ? '这个作者在所有来源中的作品' : '你订阅作者的作品，都在这里。'}</p>
          </div>
          <div className="topbar-tools">
            <button className="radar-button" onClick={() => setRadarOpen(true)}>动态雷达{socialStatus?.unread_count ? <span>{socialStatus.unread_count}</span> : null}</button>
            <div className="search-box">
              <span aria-hidden="true">⌕</span>
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索已发现作品" aria-label="搜索作品" />
            </div>
          </div>
        </header>

        {(error || failedJob) && <div className="alert">{error ?? `最近任务失败：${failedJob?.error}`}</div>}
        {activeJobs.length > 0 && <div className="sync-banner"><span className="spinner" /> 正在向来源查询，作品会自动出现…</div>}
        {suggestions.length > 0 && <button className="review-banner" onClick={() => setReviewOpen(true)}>有 {suggestions.length} 个相似作品等待确认聚合 <span>去审核 →</span></button>}
        {jobs.filter((job) => job.kind === 'download_chapter' && job.status === 'succeeded' && job.payload.output_path).slice(0, 1).map((job) => (
          <div className="download-ready" key={job.id}>CBZ 已准备好 <a href={`/api/jobs/${job.id}/file`}>保存到本机 ↓</a></div>
        ))}

        <section className="section-head">
          <div><h2>发现的作品</h2><span>{visibleWorks.length}</span></div>
          <button onClick={() => void load()} disabled={busy}>刷新视图</button>
        </section>

        {visibleWorks.length > 0 ? (
          <section className="work-grid">{visibleWorks.map((work) => <WorkCard work={work} onOpen={(item) => void openWork(item)} key={work.id} />)}</section>
        ) : (
          <section className="empty-state">
            <div className="empty-symbol">冊</div>
            <h2>{authors.length ? '还没有发现作品' : '从一位作者开始'}</h2>
            <p>{authors.length ? '来源查询可能仍在进行，也可以点击作者旁的刷新按钮重试。' : '在左侧输入作者名，我们会在已启用来源中建立他的作品档案。'}</p>
          </section>
        )}
      </main>
      {openedWork && <WorkDetail group={openedWork} allGroups={works} busy={busy} enabledProviders={sources.map((source) => source.name)} onClose={() => setOpenedWork(null)} onDownload={(edition, source) => void downloadEdition(edition, source)} onSplit={(workId) => void splitEdition(workId)} onMerge={(targetId) => void mergeCurrentInto(targetId)} />}
      {reviewOpen && <MergeReview suggestions={suggestions} agentStatus={agentStatus} busy={busy} onRunAgent={() => void act(() => api.runAgentReviews())} onOpenGroup={(id) => void openWork(id)} onClose={() => setReviewOpen(false)} onAccept={(id) => void reviewSuggestion(id, true)} onReject={(id) => void reviewSuggestion(id, false)} />}
      {radarOpen && socialStatus && <SocialRadar status={socialStatus} signals={socialSignals} authors={authors} works={works} selectedAuthorId={selectedId} busy={busy} focusSignalId={focusSignalId} onClose={() => setRadarOpen(false)} onChanged={load} onOpenWork={(id) => { setRadarOpen(false); void openWork(id) }} />}
    </div>
  )
}
