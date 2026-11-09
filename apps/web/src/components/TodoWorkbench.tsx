import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowClockwise, Books, CheckCircle, GitMerge, WarningCircle } from '@phosphor-icons/react'
import { api } from '../lib/api'
import type { CatalogEvent, FailedDownload, MergeSuggestion, PlannedRelease, WorkGroup } from '../types'

type Tab = 'all' | 'discovery' | 'merges' | 'planned' | 'failures'
type LoadKey = 'discovery' | 'merges' | 'planned' | 'failures'
const labels: Record<LoadKey, string> = { discovery: '新发现', merges: '聚合候选', planned: '待发布命中', failures: '下载失败' }
const date = (value: string | null) => value && !Number.isNaN(new Date(value).getTime()) ? new Date(value).toLocaleString('zh-CN') : '时间未知'

export function TodoWorkbench({ works, onOpenWork, onOpenReviews, onChanged }: {
  works: WorkGroup[]
  onOpenWork: (id: number) => void
  onOpenReviews: () => void
  onChanged: () => void
}) {
  const [tab, setTab] = useState<Tab>('all')
  const [events, setEvents] = useState<CatalogEvent[]>([])
  const [merges, setMerges] = useState<MergeSuggestion[]>([])
  const [planned, setPlanned] = useState<PlannedRelease[]>([])
  const [failures, setFailures] = useState<FailedDownload[]>([])
  const [moreFailures, setMoreFailures] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const [failed, setFailed] = useState<LoadKey[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [selected, setSelected] = useState<number[]>([])
  const generation = useRef(0)

  const load = useCallback(async () => {
    const current = ++generation.current
    setLoading(true)
    const results = await Promise.allSettled([api.discoveryInbox(), api.mergeSuggestions(), api.plannedMatches(), api.failedDownloads()] as const)
    if (current !== generation.current) return
    const errors: LoadKey[] = []
    const [discovery, merge, plan, downloads] = results
    if (discovery.status === 'fulfilled') { setEvents(discovery.value); setOffset(discovery.value.length); setHasMore(discovery.value.length === 100) }
    else { setEvents([]); setHasMore(false); errors.push('discovery') }
    if (merge.status === 'fulfilled') setMerges(merge.value); else { setMerges([]); errors.push('merges') }
    if (plan.status === 'fulfilled') setPlanned(plan.value); else { setPlanned([]); errors.push('planned') }
    if (downloads.status === 'fulfilled') { setFailures(downloads.value.items); setMoreFailures(downloads.value.has_more) }
    else { setFailures([]); setMoreFailures(false); errors.push('failures') }
    setSelected([]); setFailed(errors); setLoading(false)
  }, [])
  useEffect(() => { void load(); return () => { generation.current += 1 } }, [load])

  async function act(action: () => Promise<unknown>, success: string) {
    setBusy(true); setMessage('')
    try { await action(); setMessage(success); await load(); onChanged() }
    catch (error) { setMessage(error instanceof Error ? error.message : '操作失败，请重试') }
    finally { setBusy(false) }
  }
  async function more() {
    setBusy(true); setMessage('')
    try {
      const next = await api.discoveryInbox('unread', offset)
      setEvents((current) => [...current, ...next.filter((event) => !current.some((old) => old.id === event.id))])
      setOffset((current) => current + next.length); setHasMore(next.length === 100)
    } catch { setMessage('加载更多新发现失败，请重试') }
    finally { setBusy(false) }
  }
  async function bulk(state: 'read' | 'ignored') {
    if (!selected.length) return
    if (!window.confirm(`确认将选中的 ${selected.length} 条新发现标为${state === 'read' ? '已读' : '忽略'}？`)) return
    setBusy(true); setMessage('')
    let succeeded = 0
    let attempted = 0
    try {
      for (let i = 0; i < selected.length; i += 500) {
        const ids = selected.slice(i, i + 500)
        const result = await api.updateSelectedDiscoveryEvents(ids, state)
        succeeded += result.changed
        attempted += ids.length
      }
      const unchanged = selected.length - succeeded
      setMessage(unchanged ? `已处理 ${succeeded} 条，${unchanged} 条状态已变化或不存在。` : `已处理 ${succeeded} 条新发现。`)
    } catch {
      setMessage(`已处理 ${succeeded} 条，剩余 ${selected.length - attempted} 条处理失败，请重试。`)
    }
    await load(); onChanged()
    setBusy(false)
  }
  const counts: Record<LoadKey, number> = { discovery: events.length, merges: merges.length, planned: planned.length, failures: failures.length }
  const tabs: Array<{ id: Tab, title: string }> = [{ id: 'all', title: '全部' }, ...Object.entries(labels).map(([id, title]) => ({ id: id as LoadKey, title }))]
  const show = (key: LoadKey) => tab === 'all' || tab === key
  const displayedEvents = tab === 'all' ? events.slice(0, 5) : events
  const allDisplayedSelected = displayedEvents.length > 0 && displayedEvents.every((event) => selected.includes(event.id))

  return <section className="todo-workbench">
    <header className="todo-header"><div><h1>待办工作台</h1><p>把发现、审核和异常放在一起，逐项处理。</p></div><button onClick={() => { setMessage(''); void load() }} disabled={loading || busy}><ArrowClockwise size={18} /> 刷新</button></header>
    <nav className="todo-tabs" aria-label="待办类型">{tabs.map(({ id, title }) => <button key={id} aria-pressed={tab === id} onClick={() => { setTab(id); setSelected([]) }}>{title}{id !== 'all' && <span>{failed.includes(id) ? '—' : `${counts[id]}${id === 'discovery' && hasMore ? '+' : ''}`}</span>}</button>)}</nav>
    {message && <div className="todo-notice" role="status">{message}</div>}
    {loading && <p className="todo-loading" role="status">正在更新待办…</p>}
    {show('discovery') && <section className="todo-section" aria-label="新发现">
      <header><div><h2>新发现</h2><p>新作品和新版本，处理后会从待办中移除。</p></div><span>{events.length} 条已加载{hasMore ? '，还有更多' : ''}</span></header>
      {failed.includes('discovery') ? <p className="todo-problem">新发现加载失败，请刷新重试。</p> : <>
        {events.length > 0 && <div className="todo-bulk"><label><input type="checkbox" checked={allDisplayedSelected} onChange={(e) => setSelected(e.target.checked ? displayedEvents.map((event) => event.id) : [])} /> 全选当前展示</label><span>已选 {selected.length} 条</span><button disabled={!selected.length || busy} onClick={() => void bulk('read')}>标为已读</button><button disabled={!selected.length || busy} onClick={() => void bulk('ignored')}>忽略所选</button></div>}
        <div className="todo-list">{displayedEvents.map((event) => <article className="todo-row" key={event.id}>
          <input type="checkbox" aria-label={`选择 ${event.title}`} checked={selected.includes(event.id)} onChange={(e) => setSelected((old) => e.target.checked ? [...old, event.id] : old.filter((id) => id !== event.id))} />
          <div className="todo-cover">{event.cover_url ? <img src={event.cover_url} alt="" loading="lazy" /> : <Books size={22} />}</div>
          <div className="todo-copy"><small>{event.event_type === 'work_discovered' ? '新作品' : '新版本'} / {event.provider}</small><strong>{event.title}</strong><span>{event.author_name} · {date(event.created_at)}</span></div>
          <div className="todo-actions"><button disabled={!event.group_id || busy} onClick={() => event.group_id && onOpenWork(event.group_id)}>查看作品</button><button disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'interested'), '已标记感兴趣')}>感兴趣</button><button disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'read'), '已标记已读')}>已读</button><button disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'ignored'), '已忽略')}>忽略</button></div>
        </article>)}</div>
        {!events.length && !loading && <p className="todo-empty"><CheckCircle size={19} /> 暂无未处理的新发现。</p>}
        {tab === 'all' && events.length > displayedEvents.length && <button className="todo-more" onClick={() => { setSelected([]); setTab('discovery') }}>查看全部新发现</button>}
        {tab === 'discovery' && hasMore && <button className="todo-more" disabled={busy} onClick={() => void more()}>加载更多新发现</button>}
      </>}
    </section>}
    {show('merges') && <section className="todo-section" aria-label="聚合候选"><header><div><h2>聚合候选</h2><p>确认是否属于同一部作品，合并后仍可在管理记录中撤销。</p></div><button onClick={onOpenReviews}>查看完整证据</button></header>
      {failed.includes('merges') ? <p className="todo-problem">聚合候选加载失败，请刷新重试。</p> : <div className="todo-list">{merges.map((item) => <article className="todo-row" key={item.id}><GitMerge className="todo-icon" size={21} /><div className="todo-copy"><small>相似度 {Math.round(item.confidence * 100)}%</small><strong>{item.source_title} ↔ {item.target_title}</strong><span>{item.reasons.join(' / ')}{item.hard_conflicts.length ? ' / 存在硬冲突，请查看完整证据' : ''}</span></div><div className="todo-actions"><button disabled={busy} onClick={() => onOpenWork(item.source_group_id)}>查看作品</button><button disabled={busy} onClick={() => void act(() => api.rejectSuggestion(item.id), '已保持分开')}>保持分开</button><button disabled={busy} onClick={() => { if (window.confirm(`确认将《${item.source_title}》和《${item.target_title}》合并？`)) void act(() => api.acceptSuggestion(item.id), '已合并作品') }}>确认合并</button></div></article>)}{!merges.length && !loading && <p className="todo-empty"><CheckCircle size={19} /> 暂无聚合候选。</p>}</div>}
    </section>}
    {show('planned') && <section className="todo-section" aria-label="待发布命中"><header><div><h2>待发布命中</h2><p>来源发现了可能对应的作品，确认后建立关联。</p></div></header>
      {failed.includes('planned') ? <p className="todo-problem">待发布命中加载失败，请刷新重试。</p> : <div className="todo-list">{planned.map((item) => { const target = works.find((work) => work.id === item.suggested_group_id); return <article className="todo-row" key={item.id}><Books className="todo-icon" size={21} /><div className="todo-copy"><small>{item.author_name} · {date(item.updated_at)}</small><strong>{item.title}</strong><span>可能对应：{target?.title ?? `作品 #${item.suggested_group_id}`}</span></div><div className="todo-actions"><button disabled={busy || !item.suggested_group_id} onClick={() => item.suggested_group_id && onOpenWork(item.suggested_group_id)}>查看候选</button><button disabled={busy || !item.suggested_group_id} onClick={() => { if (item.suggested_group_id && window.confirm(`确认将《${item.title}》关联到《${target?.title ?? `作品 #${item.suggested_group_id}`}》？`)) void act(() => api.linkPlannedMatch(item.id, item.suggested_group_id!), '已关联作品') }}>确认关联</button></div></article> })}{!planned.length && !loading && <p className="todo-empty"><CheckCircle size={19} /> 暂无待确认命中。</p>}{planned.length === 300 && <p className="todo-hint">当前显示最近 300 条命中。</p>}</div>}
    </section>}
    {show('failures') && <section className="todo-section" aria-label="下载失败"><header><div><h2>下载失败</h2><p>仅显示同一版本最近一次仍失败的任务；重新下载前会检查 HE Manager 馆藏。</p></div></header>
      {failed.includes('failures') ? <p className="todo-problem">下载失败记录加载失败，请刷新重试。</p> : <div className="todo-list">{failures.map(({ job, work_title, group_id }) => <article className="todo-row" key={job.id}><WarningCircle className="todo-icon danger" size={21} /><div className="todo-copy"><small>{String(job.payload.provider ?? '来源未知')} · {date(job.finished_at ?? job.created_at)}</small><strong>{work_title ?? `作品 #${String(job.payload.work_id ?? '未知')}`}</strong><span>{job.error || '未返回具体原因'} · 已尝试 {job.attempts} 次</span></div><div className="todo-actions"><button disabled={busy || !group_id} onClick={() => group_id && onOpenWork(group_id)}>打开作品重新下载</button></div></article>)}{!failures.length && !loading && <p className="todo-empty"><CheckCircle size={19} /> 暂无未恢复的下载失败。</p>}{moreFailures && <p className="todo-hint">当前显示最近 100 条未恢复的下载失败。</p>}</div>}
    </section>}
  </section>
}
