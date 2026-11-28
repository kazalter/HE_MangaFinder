import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowClockwise,
  ArrowRight,
  Books,
  CalendarDots,
  Check,
  CheckCircle,
  GitMerge,
  Heart,
  Pulse,
  Sparkle,
  WarningCircle,
} from '@phosphor-icons/react'
import { api } from '../lib/api'
import type {
  AgentStatus,
  CatalogEvent,
  DashboardData,
  FailedDownload,
  MergeSuggestion,
  PlannedRelease,
  SeriesCandidate,
  WorkGroup,
} from '../types'
import { MergeReview } from './MergeReview'
import { OperationHistory } from './OperationHistory'

export type WorkspaceTab = 'discovery' | 'merges' | 'planned' | 'failures' | 'history'
type LoadKey = 'discovery' | 'planned' | 'failures'

const monthFormatter = new Intl.DateTimeFormat('zh-CN', { month: 'short' })
const workspaceDateFormatter = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
const sourceLabels: Record<string, string> = { wnacg: 'WNACG', nhentai: 'nHentai' }
const failureLabels: Record<string, string> = {
  timeout: '超时', proxy: '代理', forbidden: '访问受限', rate_limit: '限流',
  connection: '连接 / 域名', unavailable: '来源不可用', parse: '页面解析', other: '其他',
}

function relativeTime(value: string | null): string {
  if (!value) return '尚无成功记录'
  const time = new Date(value).getTime()
  if (Number.isNaN(time)) return '时间未知'
  const hours = Math.max(0, Math.round((Date.now() - time) / 3_600_000))
  if (hours < 1) return '刚刚'
  if (hours < 24) return `${hours} 小时前`
  return `${Math.round(hours / 24)} 天前`
}

function formatDate(value: string | null): string {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '时间未知' : workspaceDateFormatter.format(parsed)
}

interface Props {
  tab: WorkspaceTab
  onTabChange: (tab: WorkspaceTab) => void
  refreshKey?: number
  data: DashboardData | null
  works: WorkGroup[]
  suggestions: MergeSuggestion[]
  seriesCandidates: SeriesCandidate[]
  agentStatus: AgentStatus | null
  busy: boolean
  onReload: () => Promise<void>
  onOpenWork: (id: number) => void
  onOpenAuthorCenter: (authorId?: number, focusReleases?: boolean) => void
  onRunAgent: () => void
  onAcceptMerge: (id: number) => void
  onRejectMerge: (id: number) => void
  onCreateSeries: (candidate: SeriesCandidate) => void
  onChanged: () => Promise<void>
}

export const Workspace = memo(function Workspace({
  tab,
  onTabChange,
  refreshKey = 0,
  data,
  works,
  suggestions,
  seriesCandidates,
  agentStatus,
  busy: parentBusy,
  onReload,
  onOpenWork,
  onOpenAuthorCenter,
  onRunAgent,
  onAcceptMerge,
  onRejectMerge,
  onCreateSeries,
  onChanged,
}: Props) {
  const [events, setEvents] = useState<CatalogEvent[]>([])
  const [planned, setPlanned] = useState<PlannedRelease[]>([])
  const [failures, setFailures] = useState<FailedDownload[]>([])
  const [moreFailures, setMoreFailures] = useState(false)
  const [hasMoreEvents, setHasMoreEvents] = useState(false)
  const [offset, setOffset] = useState(0)
  const [failedKeys, setFailedKeys] = useState<LoadKey[]>([])
  const [loadingQueue, setLoadingQueue] = useState(true)
  const [localBusy, setLocalBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [selected, setSelected] = useState<number[]>([])
  const generation = useRef(0)

  const busy = parentBusy || localBusy

  const loadQueue = useCallback(async () => {
    const current = ++generation.current
    setLoadingQueue(true)
    const results = await Promise.allSettled([
      api.discoveryInbox(),
      api.plannedMatches(),
      api.failedDownloads(),
    ] as const)
    if (current !== generation.current) return
    const errors: LoadKey[] = []
    const [discoveryRes, plannedRes, failuresRes] = results
    if (discoveryRes.status === 'fulfilled') {
      setEvents(discoveryRes.value)
      setOffset(discoveryRes.value.length)
      setHasMoreEvents(discoveryRes.value.length === 100)
    } else {
      setEvents([]); setHasMoreEvents(false); errors.push('discovery')
    }
    if (plannedRes.status === 'fulfilled') setPlanned(plannedRes.value)
    else { setPlanned([]); errors.push('planned') }
    if (failuresRes.status === 'fulfilled') {
      setFailures(failuresRes.value.items)
      setMoreFailures(failuresRes.value.has_more)
    } else {
      setFailures([]); setMoreFailures(false); errors.push('failures')
    }
    setSelected([]); setFailedKeys(errors); setLoadingQueue(false)
  }, [])

  useEffect(() => {
    void loadQueue()
    return () => { generation.current += 1 }
  }, [loadQueue, refreshKey])

  async function refreshAll() {
    setNotice('')
    await Promise.all([loadQueue(), onReload()])
  }

  async function act(action: () => Promise<unknown>, success: string) {
    setLocalBusy(true); setNotice('')
    try {
      await action()
      setNotice(success)
      await Promise.all([loadQueue(), onChanged()])
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : '操作失败，请重试')
    } finally {
      setLocalBusy(false)
    }
  }

  async function loadMoreEvents() {
    setLocalBusy(true); setNotice('')
    try {
      const next = await api.discoveryInbox('unread', offset)
      setEvents((current) => [...current, ...next.filter((item) => !current.some((old) => old.id === item.id))])
      setOffset((current) => current + next.length)
      setHasMoreEvents(next.length === 100)
    } catch {
      setNotice('加载更多新发现失败，请重试')
    } finally {
      setLocalBusy(false)
    }
  }

  async function bulkDiscovery(state: 'read' | 'ignored') {
    if (!selected.length) return
    if (!window.confirm(`确认将选中的 ${selected.length} 条新发现标为${state === 'read' ? '已读' : '忽略'}？`)) return
    setLocalBusy(true); setNotice('')
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
      setNotice(unchanged ? `已处理 ${succeeded} 条，${unchanged} 条状态已变化或不存在。` : `已处理 ${succeeded} 条新发现。`)
    } catch {
      setNotice(`已处理 ${succeeded} 条，剩余 ${selected.length - attempted} 条处理失败，请重试。`)
    }
    await Promise.all([loadQueue(), onChanged()])
    setLocalBusy(false)
  }

  async function readAllDiscoveries() {
    await act(() => api.updateAllDiscoveryEvents(), '已将全部未读新发现标为已读')
  }

  const summary = data?.summary
  const trend = data?.trend ?? []
  const sourceHealth = data?.source_health ?? []
  const upcoming = data?.upcoming ?? []
  const visibleTrend = useMemo(() => trend.slice(-14), [trend])
  const trendMaximum = useMemo(
    () => Math.max(1, ...trend.map((item) => item.works + item.editions)),
    [trend],
  )
  const selectedSet = useMemo(() => new Set(selected), [selected])
  const workById = useMemo(() => new Map(works.map((work) => [work.id, work])), [works])
  const allSelected = events.length > 0 && events.every((item) => selectedSet.has(item.id))

  const discoveryCount = failedKeys.includes('discovery') ? (summary?.unread_discoveries ?? 0) : events.length
  const mergeCount = suggestions.length + seriesCandidates.length
  const signalAndPlannedCount = (summary?.pending_signals ?? 0) + planned.length
  const failureCount = failures.length + (summary?.failed_provider_scans ?? 0)

  const tabs: Array<{ id: WorkspaceTab; label: string; count?: string }> = [
    { id: 'discovery', label: '新发现收件箱', count: failedKeys.includes('discovery') ? '—' : `${events.length}${hasMoreEvents ? '+' : ''}` },
    { id: 'merges', label: '聚合审核', count: String(mergeCount) },
    { id: 'planned', label: '待发布与情报', count: failedKeys.includes('planned') ? '—' : String(planned.length + upcoming.length) },
    { id: 'failures', label: '异常与健康', count: failedKeys.includes('failures') ? '—' : String(failureCount) },
    { id: 'history', label: '整理记录' },
  ]

  function switchTab(next: WorkspaceTab) {
    setSelected([])
    onTabChange(next)
  }

  return (
    <section className="dashboard-page">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">LIBRARY / WORKSPACE</p>
          <h1>工作台</h1>
          <p>先处理新发现与聚合审核，再查看来源健康与近期发布动态。</p>
        </div>
        <button onClick={() => void refreshAll()} disabled={loadingQueue || busy}>
          <ArrowClockwise size={18} />刷新工作台
        </button>
      </header>

      <section className="dashboard-action-grid" aria-label="待处理概览">
        <button className={discoveryCount > 0 ? 'accent' : ''} onClick={() => switchTab('discovery')}>
          <Sparkle size={24} weight="fill" />
          <div><strong>{discoveryCount}{hasMoreEvents ? '+' : ''}</strong><span>个新发现待查看</span></div>
          <ArrowRight size={17} />
        </button>
        <button onClick={() => switchTab('merges')}>
          <GitMerge size={24} />
          <div><strong>{mergeCount}</strong><span>项待聚合审核</span></div>
          <ArrowRight size={17} />
        </button>
        <button onClick={() => switchTab('planned')}>
          <CalendarDots size={24} />
          <div><strong>{signalAndPlannedCount}</strong><span>条情报与待发布命中</span></div>
          <ArrowRight size={17} />
        </button>
        <button className={failureCount > 0 ? 'danger' : ''} onClick={() => switchTab('failures')}>
          <WarningCircle size={24} />
          <div><strong>{failureCount}</strong><span>项下载失败或扫描异常</span></div>
          <ArrowRight size={17} />
        </button>
      </section>

      <div className="dashboard-main-grid">
        <section className="dashboard-panel workspace-queue-panel">
          <nav className="workspace-tabs" aria-label="工作台视图">
            {tabs.map((item) => (
              <button
                key={item.id}
                className={tab === item.id ? 'active' : ''}
                aria-pressed={tab === item.id}
                onClick={() => switchTab(item.id)}
              >
                {item.label}
                {item.count !== undefined && <span>{item.count}</span>}
              </button>
            ))}
          </nav>

          {notice && <div className="workspace-notice" role="status">{notice}</div>}

          {tab === 'discovery' && (
            <section className="workspace-tab-body" aria-label="新发现收件箱">
              {failedKeys.includes('discovery') ? (
                <p className="workspace-error">新发现加载失败，请刷新重试。</p>
              ) : (
                <>
                  {events.length > 0 && (
                    <div className="todo-bulk">
                      <label>
                        <input
                          type="checkbox"
                          checked={allSelected}
                          onChange={(e) => setSelected(e.target.checked ? events.map((item) => item.id) : [])}
                        />
                        全选当前展示
                      </label>
                      <span>已选 {selected.length} 条</span>
                      <button disabled={!selected.length || busy} onClick={() => void bulkDiscovery('read')}>批量标为已读</button>
                      <button disabled={!selected.length || busy} onClick={() => void bulkDiscovery('ignored')}>批量忽略</button>
                      <button className="quiet-link" disabled={busy} onClick={() => void readAllDiscoveries()}>全部标为已读</button>
                    </div>
                  )}
                  <div className="discovery-inbox-list">
                    {events.map((event) => (
                      <article key={event.id}>
                        <input
                          type="checkbox"
                          aria-label={`选择 ${event.title}`}
                          checked={selectedSet.has(event.id)}
                          onChange={(e) => setSelected((old) => (
                            e.target.checked ? [...old, event.id] : old.filter((id) => id !== event.id)
                          ))}
                        />
                        <button
                          className="inbox-cover"
                          disabled={!event.group_id}
                          onClick={() => event.group_id && onOpenWork(event.group_id)}
                          aria-label={`打开《${event.title}》`}
                        >
                          {event.cover_url
                            ? <img src={event.cover_url} alt="" loading="lazy" decoding="async" width={48} height={62} />
                            : <Books size={24} />}
                        </button>
                        <div className="inbox-copy">
                          <span>{event.event_type === 'work_discovered' ? '新作品' : '新版本'} · {sourceLabels[event.provider] ?? event.provider}</span>
                          <h3>{event.title}</h3>
                          <small>{event.author_name} · {formatDate(event.created_at)}</small>
                        </div>
                        <div className="inbox-actions">
                          <button disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'interested'), '已标记感兴趣')} aria-label="感兴趣"><Heart size={16} />感兴趣</button>
                          <button disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'read'), '已标记已读')} aria-label="标为已读"><Check size={16} />已读</button>
                          <button className="quiet" disabled={busy} onClick={() => void act(() => api.updateDiscoveryEvent(event.id, 'ignored'), '已忽略')}>忽略</button>
                        </div>
                      </article>
                    ))}
                    {!events.length && !loadingQueue && (
                      <div className="dashboard-empty">
                        <CheckCircle size={30} />
                        <strong>收件箱已清空</strong>
                        <p>下次扫描发现新作品或新版本时会出现在这里。</p>
                      </div>
                    )}
                  </div>
                  {hasMoreEvents && (
                    <button className="workspace-load-more" disabled={busy} onClick={() => void loadMoreEvents()}>加载更多新发现</button>
                  )}
                </>
              )}
            </section>
          )}

          {tab === 'merges' && (
            <section className="workspace-tab-body workspace-padded" aria-label="聚合审核">
              {seriesCandidates.length > 0 && (
                <div className="workspace-series-list">
                  {seriesCandidates.map((candidate) => (
                    <div className="review-banner" key={`${candidate.normalized_title}:${candidate.author_ids.join('-')}`}>
                      <div>
                        <strong>系列候选「{candidate.title}」</strong>
                        <span>{candidate.groups.length} 卷{candidate.missing_volumes.length ? ` · 可能缺少第 ${candidate.missing_volumes.join('、')} 卷` : ''}</span>
                      </div>
                      <button
                        disabled={busy}
                        onClick={() => {
                          const gap = candidate.missing_volumes.length ? `，可能缺少第 ${candidate.missing_volumes.join('、')} 卷` : ''
                          if (window.confirm(`将 ${candidate.groups.length} 部作品建立为“${candidate.title}”系列${gap}？`)) {
                            onCreateSeries(candidate)
                          }
                        }}
                      >
                        建立系列 →
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <MergeReview
                suggestions={suggestions}
                agentStatus={agentStatus}
                busy={busy}
                onRunAgent={onRunAgent}
                onOpenGroup={onOpenWork}
                onAccept={onAcceptMerge}
                onReject={onRejectMerge}
              />
            </section>
          )}

          {tab === 'planned' && (
            <section className="workspace-tab-body" aria-label="待发布与情报">
              {(summary?.pending_signals ?? 0) > 0 && (
                <div className="workspace-signal-banner">
                  <span>作者动态中有 {summary?.pending_signals} 条作品情报等待人工确认</span>
                  <button onClick={() => onOpenAuthorCenter(undefined, true)}>前往作者中心处理 →</button>
                </div>
              )}
              {failedKeys.includes('planned') ? (
                <p className="workspace-error">待发布命中加载失败，请刷新重试。</p>
              ) : (
                <>
                  <header className="workspace-subhead">
                    <div>
                      <h3>待发布匹配候选</h3>
                      <p>站点抓到了可能对应的预约/预告作品，确认后自动建立关联。</p>
                    </div>
                  </header>
                  <div className="todo-list">
                    {planned.map((item) => {
                      const target = item.suggested_group_id !== null ? workById.get(item.suggested_group_id) : undefined
                      return (
                        <article className="todo-row" key={item.id}>
                          <Books className="todo-icon" size={21} />
                          <div className="todo-copy">
                            <small>{item.author_name} · {formatDate(item.updated_at)}</small>
                            <strong>{item.title}</strong>
                            <span>可能对应：{target?.title ?? `作品 #${item.suggested_group_id}`}</span>
                          </div>
                          <div className="todo-actions">
                            <button disabled={busy || !item.suggested_group_id} onClick={() => item.suggested_group_id && onOpenWork(item.suggested_group_id)}>查看候选</button>
                            <button
                              className="primary-inline"
                              disabled={busy || !item.suggested_group_id}
                              onClick={() => {
                                if (item.suggested_group_id && window.confirm(`确认将《${item.title}》关联到《${target?.title ?? `作品 #${item.suggested_group_id}`}》？`)) {
                                  void act(() => api.linkPlannedMatch(item.id, item.suggested_group_id!), '已关联作品')
                                }
                              }}
                            >
                              确认关联
                            </button>
                          </div>
                        </article>
                      )
                    })}
                    {!planned.length && !loadingQueue && (
                      <div className="dashboard-empty compact"><CheckCircle size={24} /><p>暂无待确认的发布匹配。</p></div>
                    )}
                    {planned.length === 300 && <p className="todo-hint">当前显示最近 300 条命中。</p>}
                  </div>
                </>
              )}

              <header className="workspace-subhead with-border">
                <div>
                  <h3>近期预告日历</h3>
                  <p>带明确发售日期的作者动态与展会摊位预告。</p>
                </div>
                <button className="text-link-btn" onClick={() => onOpenAuthorCenter(undefined, true)}>去作者中心查看详情 →</button>
              </header>
              <div className="upcoming-list-wide">
                {upcoming.map((item) => (
                  <article key={item.id}>
                    <time><strong>{new Date(item.release_date).getDate()}</strong><span>{monthFormatter.format(new Date(item.release_date))}</span></time>
                    <div>
                      <b>{item.title ?? '标题尚未确认'}</b>
                      <small>{item.author_name}{item.event_code ? ` · ${item.event_code}` : ''}{item.booth ? ` · ${item.booth}` : ''}</small>
                    </div>
                    {item.linked_group_id
                      ? <button onClick={() => onOpenWork(item.linked_group_id!)}>打开作品</button>
                      : <button onClick={() => onOpenAuthorCenter(item.author_id, true)}>作者动态</button>}
                  </article>
                ))}
                {!upcoming.length && <div className="dashboard-empty compact"><CalendarDots size={24} /><p>暂时没有带明确日期的待发布作品。</p></div>}
              </div>
            </section>
          )}

          {tab === 'failures' && (
            <section className="workspace-tab-body" aria-label="异常与健康">
              <header className="workspace-subhead">
                <div>
                  <h3>未恢复的下载失败</h3>
                  <p>仅展示同一版本最近一次仍失败的下载任务；点击可直接打开重新下载。</p>
                </div>
              </header>
              {failedKeys.includes('failures') ? (
                <p className="workspace-error">下载失败记录加载失败，请刷新重试。</p>
              ) : (
                <div className="todo-list">
                  {failures.map(({ job, work_title, group_id }) => (
                    <article className="todo-row" key={job.id}>
                      <WarningCircle className="todo-icon danger" size={21} />
                      <div className="todo-copy">
                        <small>{String(job.payload.provider ?? '来源未知')} · {formatDate(job.finished_at ?? job.created_at)}</small>
                        <strong>{work_title ?? `作品 #${String(job.payload.work_id ?? '未知')}`}</strong>
                        <span>{job.error || '未返回具体原因'} · 已尝试 {job.attempts} 次</span>
                      </div>
                      <div className="todo-actions">
                        <button disabled={busy || !group_id} onClick={() => group_id && onOpenWork(group_id)}>打开作品重新下载</button>
                      </div>
                    </article>
                  ))}
                  {!failures.length && !loadingQueue && (
                    <div className="dashboard-empty compact"><CheckCircle size={24} /><p>暂无未恢复的下载失败。</p></div>
                  )}
                  {moreFailures && <p className="todo-hint">当前显示最近 100 条未恢复的下载失败。</p>}
                </div>
              )}

              <header className="workspace-subhead with-border">
                <div>
                  <h3>来源扫描健康度</h3>
                  <p>各漫画站点近期抓取成功率与退避状态。</p>
                </div>
              </header>
              <div className="source-health-panel-body">
                {sourceHealth.map((source) => (
                  <article key={source.provider}>
                    <span className={`source-health-dot ${source.current_failed_authors > 0 ? 'warn' : 'ok'}`} />
                    <div>
                      <strong>{sourceLabels[source.provider] ?? source.provider}</strong>
                      <small>
                        {source.scans} 次扫描（含重试） · 新增 {source.new_items} 项 · 最近成功 {relativeTime(source.last_success_at)}
                        {source.current_failed_authors > 0 ? ` · ${source.current_failed_authors} 位作者最近扫描失败（统计周期内）` : ''}
                      </small>
                      {Object.keys(source.failure_categories ?? {}).length > 0 && (
                        <small>失败分类（含重试）：{Object.entries(source.failure_categories).map(([category, count]) => `${failureLabels[category] ?? category} ${count}`).join(' · ')}</small>
                      )}
                      {source.retry_paused_until && new Date(source.retry_paused_until).getTime() > Date.now() && (
                        <small>自动重试暂缓至 {new Date(source.retry_paused_until).toLocaleString('zh-CN')}，随后自动探测；手动刷新不受影响</small>
                      )}
                      {source.last_error && <em>{source.last_error}</em>}
                    </div>
                    <b>{Math.round(source.success_rate * 100)}%</b>
                  </article>
                ))}
                {!sourceHealth.length && <div className="dashboard-empty compact"><Pulse size={24} /><p>完成一次作品扫描后显示来源健康度。</p></div>}
              </div>
            </section>
          )}

          {tab === 'history' && (
            <section className="workspace-tab-body workspace-padded" aria-label="整理记录">
              <OperationHistory onChanged={async () => { await refreshAll(); await onChanged() }} />
            </section>
          )}
        </section>

        <aside className="workspace-aside">
          <section className="dashboard-panel library-summary-panel">
            <header><div><span>COLLECTION</span><h2>馆藏概况</h2></div></header>
            <div className="library-summary-numbers">
              <div><strong>{summary?.works ?? works.length}</strong><span>作品</span></div>
              <div><strong>{summary?.editions ?? 0}</strong><span>版本</span></div>
              <div><strong>{summary?.authors ?? 0}</strong><span>作者</span></div>
            </div>
            <dl>
              <div><dt>最近 {data?.period_days ?? 30} 天新作品</dt><dd>+{summary?.new_works ?? 0}</dd></div>
              <div><dt>最近 {data?.period_days ?? 30} 天新版本</dt><dd>+{summary?.new_editions ?? 0}</dd></div>
              <div><dt>资料不完整作品</dt><dd>{summary?.incomplete_works ?? 0}</dd></div>
            </dl>
          </section>

          <section className="dashboard-panel trend-panel">
            <header><div><span>LAST 14 DAYS</span><h2>发现趋势</h2></div><small>深色作品 · 浅色版本</small></header>
            <div className="trend-chart" aria-label="最近十四天发现趋势">
              {visibleTrend.map((item) => (
                <div className="trend-column" key={item.date} title={`${item.date}：${item.works} 部作品，${item.editions} 个版本`}>
                  <div className="trend-bars" style={{ height: `${Math.max(4, ((item.works + item.editions) / trendMaximum) * 100)}%` }}>
                    <i style={{ flex: item.editions }} />
                    <b style={{ flex: item.works }} />
                  </div>
                  <span>{new Date(`${item.date}T00:00:00`).getDate()}</span>
                </div>
              ))}
              {!visibleTrend.length && <div className="dashboard-empty compact"><p>暂无近期趋势数据</p></div>}
            </div>
          </section>
        </aside>
      </div>
    </section>
  )
})
