import { Article, ArrowsClockwise, MagnifyingGlass, NewspaperClipping, UserCircle } from '@phosphor-icons/react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { ActivityItem, Author, AuthorDigest, ReleaseSignal, SocialAccount, SocialAccountSuggestion, SocialPost, SocialStatus, WorkGroup } from '../types'
import { ThemedSelect } from './ThemedSelect'

const kindLabels: Record<string, string> = {
  new_release: '新作预告', release_preview: '样张公开', cover_reveal: '封面公开',
  event_participation: '参展动态', preorder: '预售', on_sale: '正式发售',
  reprint: '再版 / 旧刊', delay: '延期', cancellation: '取消', other: '其他',
}

const activityLabels: Record<string, string> = {
  creation_progress: '创作进度', release: '作品动态', event: '展会活动', sales: '销售通贩',
  artwork: '插画公开', collaboration: '合作项目', schedule_notice: '重要公告',
  personal: '日常近况', other: '其他动态',
}

const postTypeOptions = [
  { value: '', label: '全部类型' }, { value: 'original', label: '原创' },
  { value: 'quote', label: '引用' }, { value: 'reply', label: '回复' },
  { value: 'retweet', label: '转推' },
] as const

export type RadarFeedTab = 'overview' | 'releases' | 'raw'

function availabilityLabel(post: SocialPost): string | null {
  if (post.availability_status === 'deleted') return '原帖已删除 · 本地归档'
  if (post.availability_status === 'protected') return '账号已转为私密 · 本地归档'
  if (post.availability_status === 'account_unavailable') return '账号暂不可用 · 本地归档'
  if (post.availability_status === 'unavailable') {
    return post.availability_reason?.startsWith('deleted_candidate:')
      ? '原帖疑似已删除 · 等待二次确认'
      : '原帖暂不可用 · 本地归档'
  }
  if (post.availability_status === 'unknown') return '原帖状态未验证'
  return null
}

interface RadarProps {
  author: Author
  status: SocialStatus
  works: WorkGroup[]
  busy: boolean
  focusSignalId: number | null
  initialFeedTab?: RadarFeedTab
  onChanged: () => Promise<void>
  onOpenWork: (id: number) => void
  onOpenAccountSetup?: () => void
}

export const SocialRadar = memo(function SocialRadar({
  author,
  status,
  works,
  busy,
  focusSignalId,
  initialFeedTab = 'overview',
  onChanged,
  onOpenWork,
  onOpenAccountSetup,
}: RadarProps) {
  const previousPending = useRef(status.processing_pending)
  const [filter, setFilter] = useState('all')
  const [view, setView] = useState<RadarFeedTab>(initialFeedTab)
  const [activities, setActivities] = useState<ActivityItem[]>([])
  const [digest, setDigest] = useState<AuthorDigest | null>(null)
  const [rawPosts, setRawPosts] = useState<SocialPost[]>([])
  const [radarSignals, setRadarSignals] = useState<ReleaseSignal[]>([])
  const [postType, setPostType] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const [localNotice, setLocalNotice] = useState<string | null>(null)
  const [links, setLinks] = useState<Record<number, number>>({})
  const [linkingSignalId, setLinkingSignalId] = useState<number | null>(null)
  const [syncing, setSyncing] = useState(false)

  useEffect(() => {
    setView(initialFeedTab)
  }, [initialFeedTab])

  const loadActivityData = useCallback(async (nextPostType = postType) => {
    const [nextActivities, nextDigest, nextPosts, nextSignals] = await Promise.all([
      api.socialActivity(author.id),
      api.socialDigest(author.id),
      api.socialPosts(author.id, nextPostType || undefined),
      api.socialRadar(author.id),
    ])
    setActivities(nextActivities)
    setDigest(nextDigest)
    setRawPosts(nextPosts)
    setRadarSignals(nextSignals)
  }, [author.id, postType])

  useEffect(() => {
    let active = true
    setLinkingSignalId(null)
    setLocalError(null)
    setLocalNotice(null)
    void Promise.all([
      api.socialActivity(author.id),
      api.socialDigest(author.id),
      api.socialPosts(author.id),
      api.socialRadar(author.id),
    ]).then(([nextActivities, nextDigest, nextPosts, nextSignals]) => {
      if (!active) return
      setActivities(nextActivities)
      setDigest(nextDigest)
      setRawPosts(nextPosts)
      setRadarSignals(nextSignals)
      setPostType('')
    }).catch((reason) => {
      if (active) setLocalError(reason instanceof Error ? reason.message : '动态加载失败')
    })
    return () => { active = false }
  }, [author.id])

  useEffect(() => {
    if (status.processing_pending === previousPending.current) return
    previousPending.current = status.processing_pending
    void loadActivityData().catch((reason) => setLocalError(String(reason)))
  }, [loadActivityData, status.processing_pending])

  useEffect(() => {
    if (!focusSignalId) return
    window.setTimeout(() => document.getElementById(`signal-${focusSignalId}`)?.scrollIntoView({ block: 'center' }), 50)
  }, [focusSignalId, radarSignals.length])

  const visibleSignals = useMemo(() => radarSignals.filter((signal) => (
    signal.author_id === author.id && (filter === 'all' || signal.status === filter || signal.kind === filter)
  )), [author.id, filter, radarSignals])

  const evidencePostById = useMemo(() => {
    const map = new Map<number, SocialPost>()
    for (const post of rawPosts) map.set(post.id, post)
    for (const post of digest?.evidence_posts ?? []) map.set(post.id, post)
    return map
  }, [digest?.evidence_posts, rawPosts])

  async function act(action: () => Promise<unknown>) {
    setLocalError(null); setLocalNotice(null); setSyncing(true)
    try {
      await action()
      await Promise.all([loadActivityData(), onChanged()])
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : '操作失败')
    } finally {
      setSyncing(false)
    }
  }

  async function sendDailyDigest() {
    setLocalError(null); setLocalNotice(null)
    try {
      const job = await api.sendDailyDigest()
      setLocalNotice(`今日作者近况日报已加入发送队列（任务 #${job.id}）`)
      await onChanged()
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : '日报发送失败')
    }
  }

  function verifyPost(post: SocialPost) {
    void api.verifySocialPost(post.id)
      .then(() => loadActivityData())
      .catch((reason) => setLocalError(reason instanceof Error ? reason.message : '原帖状态检查失败'))
  }

  return (
    <div className="author-radar-panel">
      <div className="radar-status-bar">
        <span className={status.enabled ? 'ok' : 'off'}>{status.enabled ? '雷达已启用' : '雷达未启用'}</span>
        <small>Agent {status.agent_configured ? '已连接' : '规则模式'} · 外部通知 {status.qq_configured || status.webhook_configured ? '已连接' : '未配置'}</small>
        {author.x_handle && (
          <button
            className="radar-scan-btn"
            disabled={!status.enabled || syncing || busy}
            onClick={() => void act(async () => {
              await api.syncSocial(author.id)
              setLocalNotice(`已触发 @${author.x_handle} 的动态扫描`)
            })}
          >
            <ArrowsClockwise size={15} />立即扫描动态
          </button>
        )}
      </div>

      {!status.enabled && <div className="radar-warning">请在 Dockge 中设置 MANGAFINDER_SOCIAL_ENABLED=true，并启用 social Compose profile。</div>}
      {!author.x_handle && onOpenAccountSetup && (
        <div className="radar-bind-guide">
          <span>该作者尚未绑定 X 账号，绑定后可自动收集动态与发售情报。</span>
          <button onClick={onOpenAccountSetup}>前往绑定 X 账号 →</button>
        </div>
      )}
      {localError && <div className="alert">{localError}</div>}
      {localNotice && <div className="notice">{localNotice}</div>}

      <nav className="activity-tabs" aria-label="动态中心视图">
        <button className={view === 'overview' ? 'active' : ''} onClick={() => setView('overview')}>最近近况</button>
        <button className={view === 'releases' ? 'active' : ''} onClick={() => setView('releases')}>
          作品情报{radarSignals.filter((s) => s.status === 'pending').length > 0 ? ` (${radarSignals.filter((s) => s.status === 'pending').length})` : ''}
        </button>
        <button className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>原始动态</button>
      </nav>

      {(status.processing_pending ?? 0) > 0 && <p className="radar-warning">{status.processing_pending} 条动态正在后台分析，摘要会随处理完成更新。</p>}
      {(status.processing_retry ?? 0) > 0 && <p className="radar-warning">{status.processing_retry} 条动态或图片等待重试，原始帖子已保留。</p>}

      {view === 'overview' && <>
        <section className="digest-card">
          <div className="digest-heading">
            <div>
              <span>ROLLING 7 DAYS</span>
              <h3>最近 7 天摘要</h3>
              {status.daily_digest_enabled && <small>日报每天 {String(status.daily_digest_hour).padStart(2, '0')}:00（{status.daily_digest_timezone}）通过已启用渠道发送</small>}
            </div>
            <div className="digest-buttons">
              {(status.qq_configured || status.webhook_configured) && status.daily_digest_enabled && (
                <button onClick={() => void sendDailyDigest()}>立即发送今日日报</button>
              )}
              <button onClick={() => void act(async () => { setDigest(await api.refreshSocialDigest(author.id)) })}>重新总结</button>
            </div>
          </div>
          {digest ? <>
            <p className="digest-evidence-note">摘要基于采集的动态生成，判断与翻译请结合原帖核对。</p>
            <p className="digest-summary">{digest.summary}</p>
            <div className="digest-highlights">
              {digest.highlights.map((item, index) => (
                <article className={`importance-${item.importance}`} key={`${item.text}-${index}`}>
                  <span>{activityLabels[item.category] ?? item.category} · {item.factuality === 'fact' ? '来源事实（待核对）' : item.factuality === 'plan' ? '作者计划' : '推测'}</span>
                  <p>{item.text}</p>
                  <small>
                    证据 {item.post_ids.map((id, evidenceIndex) => {
                      const post = evidencePostById.get(id)
                      return post
                        ? <span key={id}>{evidenceIndex > 0 ? '、' : ''}<a href={post.url} target="_blank" rel="noreferrer">帖子 {id} ↗</a></span>
                        : <span key={id}>{evidenceIndex > 0 ? '、' : ''}帖子 {id}</span>
                    })}
                  </small>
                </article>
              ))}
            </div>
            {digest.uncertainties.length > 0 && (
              <div className="digest-uncertain"><strong>仍不确定</strong><ul>{digest.uncertainties.map((item) => <li key={item}>{item}</li>)}</ul></div>
            )}
            <small className="digest-source">{digest.generated_by === 'agent' ? `Agent 总结 · ${digest.model ?? ''}` : '规则降级摘要'}{digest.error ? ` · Agent 暂不可用：${digest.error}` : ''}</small>
          </> : <p className="review-empty">扫描到作者动态后，这里会生成最近 7 天摘要。</p>}
        </section>

        <section className="activity-list">
          {activities.map((item) => {
            const post = item.posts.at(-1)
            const image = post?.media.find((media) => media.type === 'image')?.url
            const availability = post ? availabilityLabel(post) : null
            return (
              <article className={`${item.is_read ? '' : 'unread'} importance-${item.importance}`} key={item.id}>
                <div className="activity-thumb">{image ? <img src={image} alt="" loading="lazy" decoding="async" /> : <Article size={30} weight="thin" />}</div>
                <div>
                  <div className="activity-meta"><b>{activityLabels[item.category] ?? item.category}</b><span>{item.author_name}</span><time>{new Date(item.ended_at).toLocaleString()}</time></div>
                  <h3>{item.headline}</h3>
                  <p>{item.summary}</p>
                  {availability && <small className="post-availability">{availability}</small>}
                  <div className="activity-actions">
                    <span>{item.importance === 'critical' ? '紧急' : item.importance === 'high' ? '重要' : item.importance === 'normal' ? '一般' : '低优先级'} · 置信度 {Math.round(item.confidence * 100)}%</span>
                    {post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => { void api.markActivityRead(item.id); verifyPost(post) }}>查看原帖 ↗</a>}
                  </div>
                </div>
              </article>
            )
          })}
          {!activities.length && <div className="review-empty">还没有可展示的作者近况。</div>}
        </section>
      </>}

      {view === 'releases' && <>
        <nav className="radar-filters" aria-label="作品情报筛选">
          {[['all', '全部'], ['pending', '待确认'], ['confirmed', '已确认'], ['new_release', '新作'], ['event_participation', '参展'], ['rejected', '已排除']].map(([value, label]) => (
            <button className={filter === value ? 'active' : ''} onClick={() => setFilter(value)} key={value}>{label}</button>
          ))}
        </nav>
        <section className="signal-list">
          {visibleSignals.map((signal) => {
            const post = signal.posts.at(-1)
            const image = post?.media.find((item) => item.type === 'image' && item.url)?.url
            return (
              <article className={`${signal.is_read ? '' : 'unread'} ${focusSignalId === signal.id ? 'focused' : ''}`} id={`signal-${signal.id}`} key={signal.id}>
                <div className="signal-media">{image ? <img src={image} alt="帖子媒体" loading="lazy" decoding="async" /> : <NewspaperClipping size={36} weight="thin" />}</div>
                <div className="signal-copy">
                  <div className="signal-meta"><span className={`signal-kind kind-${signal.kind}`}>{kindLabels[signal.kind] ?? signal.kind}</span><b>{signal.author_name}</b><time>{new Date(post?.posted_at ?? signal.created_at).toLocaleString()}</time></div>
                  <h3>{signal.title ?? '标题尚未确认'}</h3>
                  <p className="signal-post-text">{post?.text || post?.ocr_text || '帖子没有可读取的文字'}</p>
                  {post && availabilityLabel(post) && <small className="post-availability">{availabilityLabel(post)}</small>}
                  <div className="signal-facts"><span>置信度 {Math.round(signal.confidence * 100)}%</span>{signal.event_code && <span>{signal.event_code}</span>}{signal.booth && <span>摊位 {signal.booth}</span>}<span>状态 {signal.status}</span></div>
                  <div className="signal-evidence">
                    <strong>支持证据</strong>
                    {signal.evidence.length ? <ul>{signal.evidence.map((item) => <li key={item}>{item}</li>)}</ul> : <p>没有足够证据</p>}
                    {signal.counter_evidence.length > 0 && <><strong className="counter">反证 / 风险</strong><ul>{signal.counter_evidence.map((item) => <li key={item}>{item}</li>)}</ul></>}
                  </div>
                  <div className="signal-actions">
                    {post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => { void api.markSocialSignalRead(signal.id); verifyPost(post) }}>查看原帖 ↗</a>}
                    {signal.linked_group_id
                      ? <button onClick={() => onOpenWork(signal.linked_group_id!)}>打开关联作品</button>
                      : linkingSignalId === signal.id
                        ? <>
                            <select autoFocus value={links[signal.id] ?? ''} onChange={(event) => setLinks({ ...links, [signal.id]: Number(event.target.value) })}>
                              <option value="">选择要关联的作品…</option>
                              {works.map((work) => <option value={work.id} key={work.id}>{work.title}</option>)}
                            </select>
                            <button disabled={!links[signal.id]} onClick={() => void act(async () => { await api.linkSocialSignal(signal.id, links[signal.id]); setLinkingSignalId(null) })}>关联</button>
                            <button onClick={() => setLinkingSignalId(null)}>取消</button>
                          </>
                        : <button onClick={() => setLinkingSignalId(signal.id)}>关联现有作品</button>}
                    {signal.status === 'pending' && <>
                      <button className="reject" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'reject'))}>不是新情报</button>
                      <button className="confirm" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'confirm'))}>确认情报</button>
                    </>}
                  </div>
                </div>
              </article>
            )
          })}
          {!visibleSignals.length && <div className="review-empty">当前筛选下没有作者动态。</div>}
        </section>
      </>}

      {view === 'raw' && (
        <section className="raw-posts">
          <div className="raw-post-filter">
            <strong>原始事实流</strong>
            <ThemedSelect label="动态类型" value={postType} options={postTypeOptions} onChange={(value) => { setPostType(value); void loadActivityData(value) }} />
          </div>
          {rawPosts.map((post) => (
            <article key={post.id}>
              <div><b>{post.post_type === 'original' ? '原创' : post.post_type === 'quote' ? '引用' : post.post_type === 'reply' ? '回复' : '转推'}</b><time>{new Date(post.posted_at).toLocaleString()}</time></div>
              <p>{post.text || post.ocr_text || '无文字内容'}</p>
              {post.media.length > 0 && (
                <div className="raw-media">{post.media.filter((item) => item.type === 'image' && item.url).slice(0, 4).map((item) => <img src={item.url} alt="" loading="lazy" decoding="async" key={item.url} />)}</div>
              )}
              {availabilityLabel(post) && <small className="post-availability">{availabilityLabel(post)}</small>}
              <a href={post.url} target="_blank" rel="noreferrer" onClick={() => verifyPost(post)}>打开原帖 ↗</a>
            </article>
          ))}
          {!rawPosts.length && <div className="review-empty">还没有采集到原始动态。</div>}
        </section>
      )}
    </div>
  )
})

export const AuthorXAccountsPanel = memo(function AuthorXAccountsPanel({
  author,
  status,
  busy,
  onChanged,
}: {
  author: Author
  status: SocialStatus
  busy: boolean
  onChanged: () => Promise<void>
}) {
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [suggestions, setSuggestions] = useState<SocialAccountSuggestion[]>([])
  const [handle, setHandle] = useState('')
  const [finding, setFinding] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [localNotice, setLocalNotice] = useState<string | null>(null)

  const loadAccounts = useCallback(async () => {
    setAccounts(await api.socialAccounts(author.id))
  }, [author.id])

  useEffect(() => {
    let active = true
    setSuggestions([]); setHandle(''); setLocalError(null); setLocalNotice(null)
    void api.socialAccounts(author.id)
      .then((rows) => { if (active) setAccounts(rows) })
      .catch((reason) => { if (active) setLocalError(reason instanceof Error ? reason.message : '账号列表加载失败') })
    return () => { active = false }
  }, [author.id])

  async function act(action: () => Promise<unknown>, successMessage?: string) {
    setLocalError(null); setLocalNotice(null)
    try {
      await action()
      await Promise.all([loadAccounts(), onChanged()])
      if (successMessage) setLocalNotice(successMessage)
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : '操作失败')
    }
  }

  async function findAccounts() {
    setFinding(true); setLocalError(null)
    try { setSuggestions(await api.socialAccountSuggestions(author.id)) }
    catch (reason) { setLocalError(reason instanceof Error ? reason.message : '查找账号失败') }
    finally { setFinding(false) }
  }

  async function bindAccount(nextHandle: string) {
    if (!nextHandle.trim()) return
    await act(() => api.addSocialAccount(author.id, nextHandle.trim()), `已绑定 @${nextHandle.trim().replace(/^@/, '')}`)
    setHandle(''); setSuggestions([])
  }

  return (
    <section className="account-panel" aria-label="X 账号绑定与同步">
      <div className="account-heading">
        <div>
          <strong>{author.name} 的 X 账号</strong>
          <small>候选账号只有经过你的确认才会监控，避免同名误绑</small>
        </div>
        <button onClick={() => void findAccounts()} disabled={!status.enabled || finding || busy}>
          <MagnifyingGlass size={15} />{finding ? '查找中…' : '自动查找候选'}
        </button>
      </div>

      {!status.enabled && <div className="radar-warning">雷达未启用：请在 Dockge 中开启 MANGAFINDER_SOCIAL_ENABLED=true 与 social profile。</div>}
      {localError && <div className="alert">{localError}</div>}
      {localNotice && <div className="notice">{localNotice}</div>}

      <div className="bound-accounts">
        {accounts.map((account) => (
          <article key={account.id}>
            {account.avatar_url ? <img src={account.avatar_url} alt="" loading="lazy" decoding="async" /> : <UserCircle size={30} weight="thin" />}
            <div>
              <a href={account.profile_url ?? `https://x.com/${account.handle}`} target="_blank" rel="noreferrer">@{account.handle}</a>
              <small>
                {account.status === 'confirmed'
                  ? `已确认 · ${account.last_synced_at ? `上次同步 ${new Date(account.last_synced_at).toLocaleString()}` : '等待首次同步'}`
                  : '待确认'}
                {account.sync_error ? ` · ${account.sync_error}` : ''}
              </small>
            </div>
            <button onClick={() => void act(() => api.deleteSocialAccount(author.id, account.id), `已移除 @${account.handle}`)}>移除</button>
          </article>
        ))}
        {!accounts.length && <small>还没有绑定 X 账号。</small>}
      </div>

      {suggestions.length > 0 && (
        <div className="account-suggestions">
          {suggestions.map((item) => (
            <button key={item.handle} onClick={() => void bindAccount(item.handle)}>
              <b>@{item.handle}</b>
              <span>{item.display_name} · {Math.round(item.score * 100)}%</span>
              <em>确认绑定</em>
            </button>
          ))}
        </div>
      )}

      <div className="manual-account">
        <input value={handle} onChange={(event) => setHandle(event.target.value)} placeholder="手动输入 @handle" disabled={!status.enabled} />
        <button disabled={!status.enabled || !handle.trim() || busy} onClick={() => void bindAccount(handle)}>确认并监控</button>
        <button
          disabled={!status.enabled || !accounts.some((item) => item.status === 'confirmed') || busy}
          onClick={() => void act(() => api.syncSocial(author.id), '已加入 X 动态扫描队列')}
        >
          立即扫描
        </button>
      </div>
    </section>
  )
})
