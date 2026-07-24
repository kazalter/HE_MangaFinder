import {
  ArrowRight,
  Books,
  CalendarDots,
  Check,
  ClockCounterClockwise,
  Heart,
  Pulse,
  Sparkle,
  WarningCircle,
} from '@phosphor-icons/react'
import type { CatalogEvent, DashboardData } from '../types'

const sourceLabels: Record<string, string> = {
  wnacg: 'WNACG',
  nhentai: 'nHentai',
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

interface Props {
  data: DashboardData
  events: CatalogEvent[]
  busy: boolean
  onReload: () => void
  onOpenWork: (id: number) => void
  onOpenRadar: () => void
  onSetEventState: (id: number, state: CatalogEvent['state']) => void
  onReadAll: () => void
  onOpenReviews: () => void
}

export function Dashboard({ data, events, busy, onReload, onOpenWork, onOpenRadar, onSetEventState, onReadAll, onOpenReviews }: Props) {
  const { summary } = data
  const trendMaximum = Math.max(1, ...data.trend.map((item) => item.works + item.editions))
  const visibleTrend = data.trend.slice(-14)

  return <section className="dashboard-page">
    <header className="dashboard-header">
      <div><p className="eyebrow">LIBRARY / OVERVIEW</p><h1>资料库总览</h1><p>先处理新发现和异常，再查看最近的馆藏变化。</p></div>
      <button onClick={onReload} disabled={busy}><ClockCounterClockwise size={18} />刷新数据</button>
    </header>

    <section className="dashboard-action-grid" aria-label="待处理概览">
      <article className="accent"><Sparkle size={25} weight="fill" /><div><strong>{summary.unread_discoveries}</strong><span>个新发现待查看</span></div></article>
      <button onClick={onOpenReviews}><Books size={25} /><div><strong>{summary.pending_merges}</strong><span>个作品等待聚合</span></div><ArrowRight size={17} /></button>
      <button onClick={onOpenRadar}><CalendarDots size={25} /><div><strong>{summary.pending_signals}</strong><span>条作品情报待确认</span></div><ArrowRight size={17} /></button>
      <article className={summary.failed_provider_scans ? 'danger' : ''}><WarningCircle size={25} /><div><strong>{summary.failed_provider_scans}</strong><span>次来源扫描失败</span></div></article>
    </section>

    <div className="dashboard-main-grid">
      <section className="dashboard-panel discovery-inbox-panel">
        <header><div><span>DISCOVERY INBOX</span><h2>新发现收件箱</h2></div>{events.length > 0 && <button onClick={onReadAll}>全部标为已读</button>}</header>
        <div className="discovery-inbox-list">
          {events.slice(0, 12).map((event) => <article key={event.id}>
            <button className="inbox-cover" disabled={!event.group_id} onClick={() => event.group_id && onOpenWork(event.group_id)} aria-label={`打开《${event.title}》`}>
              {event.cover_url ? <img src={event.cover_url} alt="" loading="lazy" /> : <Books size={24} />}
            </button>
            <div className="inbox-copy"><span>{event.event_type === 'work_discovered' ? '新作品' : '新版本'} · {sourceLabels[event.provider] ?? event.provider}</span><h3>{event.title}</h3><small>{event.author_name} · {new Date(event.created_at).toLocaleString('zh-CN')}</small></div>
            <div className="inbox-actions">
              <button onClick={() => onSetEventState(event.id, 'interested')} title="标记感兴趣" aria-label="标记感兴趣"><Heart size={17} />感兴趣</button>
              <button onClick={() => onSetEventState(event.id, 'read')} title="标为已读" aria-label="标为已读"><Check size={17} />已读</button>
              <button className="quiet" onClick={() => onSetEventState(event.id, 'ignored')}>忽略</button>
            </div>
          </article>)}
          {!events.length && <div className="dashboard-empty"><Check size={30} /><strong>收件箱已清空</strong><p>下次扫描发现新作品或新版本时会出现在这里。</p></div>}
        </div>
      </section>

      <aside className="dashboard-panel library-summary-panel">
        <header><div><span>COLLECTION</span><h2>馆藏概况</h2></div></header>
        <div className="library-summary-numbers"><div><strong>{summary.works}</strong><span>作品</span></div><div><strong>{summary.editions}</strong><span>版本</span></div><div><strong>{summary.authors}</strong><span>作者</span></div></div>
        <dl><div><dt>最近 {data.period_days} 天新作品</dt><dd>+{summary.new_works}</dd></div><div><dt>最近 {data.period_days} 天新版本</dt><dd>+{summary.new_editions}</dd></div><div><dt>资料不完整作品</dt><dd>{summary.incomplete_works}</dd></div></dl>
      </aside>
    </div>

    <div className="dashboard-lower-grid">
      <section className="dashboard-panel trend-panel">
        <header><div><span>LAST 14 DAYS</span><h2>发现趋势</h2></div><small>深色为新作品，浅色为新版本</small></header>
        <div className="trend-chart" aria-label="最近十四天发现趋势">
          {visibleTrend.map((item) => <div className="trend-column" key={item.date} title={`${item.date}：${item.works} 部作品，${item.editions} 个版本`}><div className="trend-bars" style={{ height: `${Math.max(4, ((item.works + item.editions) / trendMaximum) * 100)}%` }}><i style={{ flex: item.editions }} /><b style={{ flex: item.works }} /></div><span>{new Date(`${item.date}T00:00:00`).getDate()}</span></div>)}
        </div>
      </section>

      <section className="dashboard-panel source-health-panel">
        <header><div><span>PROVIDERS</span><h2>来源健康度</h2></div><Pulse size={21} /></header>
        <div>{data.source_health.map((source) => <article key={source.provider}><span className={`source-health-dot ${source.failed && source.success_rate < 1 ? 'warn' : 'ok'}`} /><div><strong>{sourceLabels[source.provider] ?? source.provider}</strong><small>{source.scans} 次扫描 · 新增 {source.new_items} 项 · 最近成功 {relativeTime(source.last_success_at)}</small>{source.last_error && <em>{source.last_error}</em>}</div><b>{Math.round(source.success_rate * 100)}%</b></article>)}{!data.source_health.length && <div className="dashboard-empty compact"><Pulse size={25} /><p>完成一次作品扫描后显示来源健康度。</p></div>}</div>
      </section>

      <section className="dashboard-panel upcoming-panel">
        <header><div><span>UPCOMING</span><h2>即将发布</h2></div><button onClick={onOpenRadar}>动态中心 <ArrowRight size={14} /></button></header>
        <div>{data.upcoming.slice(0, 6).map((item) => <article key={item.id}><time><strong>{new Date(item.release_date).getDate()}</strong><span>{new Date(item.release_date).toLocaleDateString('zh-CN', { month: 'short' })}</span></time><div><b>{item.title ?? '标题尚未确认'}</b><small>{item.author_name}{item.event_code ? ` · ${item.event_code}` : ''}{item.booth ? ` · ${item.booth}` : ''}</small></div>{item.linked_group_id && <button onClick={() => onOpenWork(item.linked_group_id!)}>打开</button>}</article>)}{!data.upcoming.length && <div className="dashboard-empty compact"><CalendarDots size={25} /><p>暂时没有带明确日期的待发布作品。</p></div>}</div>
      </section>
    </div>
  </section>
}
