import { Article, ArrowsClockwise, GearSix, MagnifyingGlass, NewspaperClipping, UserCircle } from '@phosphor-icons/react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { ActivityItem, Author, AuthorDigest, ReleaseSignal, SocialAccount, SocialAccountSuggestion, SocialPost, SocialStatus, WorkGroup } from '../types'

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

const radarAuthorKey = 'mangafinder:radar-author'

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

function initialRadarAuthorId(
  selectedAuthorId: number | null,
  focusSignalId: number | null,
  signals: ReleaseSignal[],
  authors: Author[],
): number | null {
  const focusedAuthorId = signals.find((signal) => signal.id === focusSignalId)?.author_id
  if (focusedAuthorId && authors.some((author) => author.id === focusedAuthorId)) return focusedAuthorId
  if (selectedAuthorId && authors.some((author) => author.id === selectedAuthorId)) return selectedAuthorId
  try {
    const stored = Number(window.localStorage.getItem(radarAuthorKey))
    if (Number.isSafeInteger(stored) && authors.some((author) => author.id === stored)) return stored
  } catch { /* The author can still be selected without preference storage. */ }
  return authors[0]?.id ?? null
}

interface Props {
  status: SocialStatus
  signals: ReleaseSignal[]
  authors: Author[]
  works: WorkGroup[]
  selectedAuthorId: number | null
  busy: boolean
  focusSignalId: number | null
  onClose: () => void
  onChanged: () => Promise<void>
  onOpenWork: (id: number) => void
}

export function SocialRadar({ status, signals, authors, works, selectedAuthorId, busy, focusSignalId, onClose, onChanged, onOpenWork }: Props) {
  const [filter, setFilter] = useState('all')
  const [view, setView] = useState<'overview' | 'releases' | 'raw'>('overview')
  const [radarAuthorId, setRadarAuthorId] = useState<number | null>(() => initialRadarAuthorId(selectedAuthorId, focusSignalId, signals, authors))
  const [authorQuery, setAuthorQuery] = useState('')
  const [accountOpen, setAccountOpen] = useState(false)
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [suggestions, setSuggestions] = useState<SocialAccountSuggestion[]>([])
  const [activities, setActivities] = useState<ActivityItem[]>([])
  const [digest, setDigest] = useState<AuthorDigest | null>(null)
  const [rawPosts, setRawPosts] = useState<SocialPost[]>([])
  const [postType, setPostType] = useState('')
  const [handle, setHandle] = useState('')
  const [finding, setFinding] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [localNotice, setLocalNotice] = useState<string | null>(null)
  const [links, setLinks] = useState<Record<number, number>>({})
  const [radarSignals, setRadarSignals] = useState<ReleaseSignal[]>(signals)
  const activeAuthor = authors.find((author) => author.id === radarAuthorId)
  const filteredAuthors = useMemo(() => {
    const term = authorQuery.trim().toLocaleLowerCase()
    return authors.filter((author) => (
      !term
      || author.name.toLocaleLowerCase().includes(term)
      || author.x_handle?.toLocaleLowerCase().includes(term)
      || author.x_display_name?.toLocaleLowerCase().includes(term)
    )).sort((left, right) => {
      if (left.id === radarAuthorId) return -1
      if (right.id === radarAuthorId) return 1
      return left.name.localeCompare(right.name, 'zh-CN')
    })
  }, [authorQuery, authors, radarAuthorId])

  async function loadAccounts() {
    if (!radarAuthorId) { setAccounts([]); return }
    setAccounts(await api.socialAccounts(radarAuthorId))
  }

  async function loadActivityData(nextPostType = postType) {
    if (!radarAuthorId) { setActivities([]); setDigest(null); setRawPosts([]); setRadarSignals([]); return }
    const [nextActivities, nextDigest, nextPosts, nextSignals] = await Promise.all([
      api.socialActivity(radarAuthorId), api.socialDigest(radarAuthorId),
      api.socialPosts(radarAuthorId, nextPostType || undefined), api.socialRadar(radarAuthorId),
    ])
    setActivities(nextActivities); setDigest(nextDigest); setRawPosts(nextPosts); setRadarSignals(nextSignals)
  }

  useEffect(() => {
    if (radarAuthorId !== null && authors.some((author) => author.id === radarAuthorId)) return
    setRadarAuthorId(authors[0]?.id ?? null)
  }, [authors, radarAuthorId])
  useEffect(() => {
    try {
      if (radarAuthorId === null) window.localStorage.removeItem(radarAuthorKey)
      else window.localStorage.setItem(radarAuthorKey, String(radarAuthorId))
    } catch { /* The radar remains usable without preference storage. */ }
  }, [radarAuthorId])
  useEffect(() => {
    setSuggestions([]); setHandle(''); setLocalError(null); setLocalNotice(null)
    void Promise.all([loadAccounts(), loadActivityData('')]).catch((error) => setLocalError(String(error)))
  }, [radarAuthorId])
  useEffect(() => {
    if (!focusSignalId) return
    window.setTimeout(() => document.getElementById(`signal-${focusSignalId}`)?.scrollIntoView({ block: 'center' }), 50)
  }, [focusSignalId])

  const visible = useMemo(() => radarSignals.filter((signal) => {
    if (radarAuthorId && signal.author_id !== radarAuthorId) return false
    return filter === 'all' || signal.status === filter || signal.kind === filter
  }), [filter, radarAuthorId, radarSignals])

  async function act(action: () => Promise<unknown>) {
    setLocalError(null); setLocalNotice(null)
    try { await action(); await Promise.all([loadAccounts(), loadActivityData(), onChanged()]) }
    catch (error) { setLocalError(error instanceof Error ? error.message : '操作失败') }
  }

  async function sendDailyDigest() {
    setLocalError(null); setLocalNotice(null)
    try {
      const job = await api.sendDailyDigest()
      setLocalNotice(`今日作者近况日报已加入发送队列（任务 #${job.id}）`)
      await onChanged()
    } catch (error) { setLocalError(error instanceof Error ? error.message : '日报发送失败') }
  }

  async function findAccounts() {
    if (!radarAuthorId) return
    setFinding(true); setLocalError(null)
    try { setSuggestions(await api.socialAccountSuggestions(radarAuthorId)) }
    catch (error) { setLocalError(error instanceof Error ? error.message : '查找账号失败') }
    finally { setFinding(false) }
  }

  async function bindAccount(nextHandle: string) {
    if (!radarAuthorId || !nextHandle.trim()) return
    await act(() => api.addSocialAccount(radarAuthorId, nextHandle.trim()))
    setHandle(''); setSuggestions([])
  }

  function verifyPost(post: SocialPost) {
    void api.verifySocialPost(post.id)
      .then(() => loadActivityData())
      .catch((error) => setLocalError(error instanceof Error ? error.message : '原帖状态检查失败'))
  }

  return (
    <div className="modal-backdrop social-radar-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}>
      <section className="social-radar" role="dialog" aria-modal="true" aria-label="作者动态雷达">
        <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        <header className="radar-header">
          <div><p className="eyebrow">AUTHOR / ACTIVITY CENTER</p><h2>作者动态中心</h2><p>汇总作者最近近况；新作与重要变更仍走独立高精度提醒。</p></div>
          <div className="radar-health">
            <span className={status.enabled ? 'ok' : 'off'}>{status.enabled ? '雷达已启用' : '雷达未启用'}</span>
            <small>Agent {status.agent_configured ? '已连接' : '规则模式'} · QQ {status.qq_configured ? '已连接' : '未配置'}</small>
          </div>
        </header>

        {!status.enabled && <div className="radar-warning">请在 Dockge 中设置 MANGAFINDER_SOCIAL_ENABLED=true，并启用 social Compose profile。</div>}
        {localError && <div className="alert">{localError}</div>}
        {localNotice && <div className="notice">{localNotice}</div>}

        <div className="radar-workspace">
          <aside className="radar-author-rail" aria-label="雷达作者选择">
            <label className="radar-author-search">
              <MagnifyingGlass size={18} />
              <span className="sr-only">搜索雷达作者</span>
              <input value={authorQuery} onChange={(event) => setAuthorQuery(event.target.value)} placeholder="搜索作者" />
            </label>
            <div className="radar-author-rail-heading"><strong>全部作者</strong><span>{authors.length}</span></div>
            <nav className="radar-author-list" aria-label="选择要查看动态的作者">
              {filteredAuthors.map((author) => {
                const active = author.id === radarAuthorId
                return <button className={active ? 'active' : ''} onClick={() => { setRadarAuthorId(author.id); setAccountOpen(false) }} aria-current={active ? 'true' : undefined} key={author.id}>
                  <span className="radar-author-avatar">
                    {author.avatar_url
                      ? <img src={author.avatar_url} alt="" referrerPolicy="no-referrer" />
                      : <UserCircle size={38} weight="thin" />}
                  </span>
                  <span className="radar-author-copy"><b>{author.name}</b><small>{author.x_handle ? `@${author.x_handle}` : '未绑定 X'}</small><em className={author.x_sync_error ? 'error' : author.x_last_synced_at ? 'ok' : ''}>{author.work_count} 部作品 · {author.x_sync_error ? '同步异常' : author.x_last_synced_at ? '同步正常' : '等待同步'}</em></span>
                </button>
              })}
              {!filteredAuthors.length && <p className="radar-author-empty">没有匹配的作者</p>}
            </nav>
            <button className="radar-account-rail-button" disabled={!activeAuthor} onClick={() => setAccountOpen((current) => !current)} aria-expanded={accountOpen}>
              <GearSix size={18} />管理 X 账号
            </button>
          </aside>

          <div className="radar-content">
            {activeAuthor ? <>
              <header className="radar-author-context">
                <span className="radar-context-avatar">
                  {activeAuthor.avatar_url
                    ? <img src={activeAuthor.avatar_url} alt="" referrerPolicy="no-referrer" />
                    : <UserCircle size={66} weight="thin" />}
                </span>
                <div><h3>{activeAuthor.name}</h3><span>{activeAuthor.x_handle ? `@${activeAuthor.x_handle}` : '未绑定 X 账号'}</span><small className={activeAuthor.x_sync_error ? 'error' : 'ok'}>{activeAuthor.work_count} 部作品 · {activeAuthor.x_sync_error ? '同步异常' : activeAuthor.x_last_synced_at ? '同步正常' : '等待首次同步'}</small></div>
                <button className={accountOpen ? 'active' : ''} onClick={() => setAccountOpen((current) => !current)} aria-expanded={accountOpen}><ArrowsClockwise size={17} />账号与同步</button>
              </header>

        {accountOpen && <section className="account-panel radar-account-panel">
          <div className="account-heading">
            <div><strong>{activeAuthor.name} 的 X 账号</strong><small>候选账号只有经过你的确认才会监控</small></div>
            <button onClick={() => void findAccounts()} disabled={!status.enabled || finding || busy}>{finding ? '查找中…' : '自动查找候选'}</button>
          </div>
            <div className="bound-accounts">
              {accounts.map((account) => <article key={account.id}>
                {account.avatar_url ? <img src={account.avatar_url} alt="" /> : <UserCircle size={30} weight="thin" />}
                <div><a href={account.profile_url ?? `https://x.com/${account.handle}`} target="_blank" rel="noreferrer">@{account.handle}</a><small>{account.status === 'confirmed' ? `已确认 · ${account.last_synced_at ? `上次同步 ${new Date(account.last_synced_at).toLocaleString()}` : '等待首次同步'}` : '待确认'}{account.sync_error ? ` · ${account.sync_error}` : ''}</small></div>
                <button onClick={() => void act(() => api.deleteSocialAccount(activeAuthor.id, account.id))}>移除</button>
              </article>)}
              {!accounts.length && <small>还没有绑定账号。</small>}
            </div>
            {suggestions.length > 0 && <div className="account-suggestions">{suggestions.map((item) => <button key={item.handle} onClick={() => void bindAccount(item.handle)}><b>@{item.handle}</b><span>{item.display_name} · {Math.round(item.score * 100)}%</span><em>确认绑定</em></button>)}</div>}
            <div className="manual-account"><input value={handle} onChange={(event) => setHandle(event.target.value)} placeholder="手动输入 @handle" disabled={!status.enabled} /><button disabled={!status.enabled || !handle.trim() || busy} onClick={() => void bindAccount(handle)}>确认并监控</button><button disabled={!status.enabled || !accounts.some((item) => item.status === 'confirmed') || busy} onClick={() => void act(() => api.syncSocial(activeAuthor.id))}>立即扫描</button></div>
        </section>}

        <nav className="activity-tabs" aria-label="动态中心视图">
          <button className={view === 'overview' ? 'active' : ''} onClick={() => setView('overview')}>最近近况</button>
          <button className={view === 'releases' ? 'active' : ''} onClick={() => setView('releases')}>作品情报</button>
          <button className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>原始动态</button>
        </nav>

        {view === 'overview' && <>
          <section className="digest-card">
            <div className="digest-heading"><div><span>ROLLING 7 DAYS</span><h3>最近 7 天摘要</h3>{status.daily_digest_enabled && <small>QQ 日报每天 {String(status.daily_digest_hour).padStart(2, '0')}:00（{status.daily_digest_timezone}）自动发送</small>}</div><div className="digest-buttons">{status.qq_configured && status.daily_digest_enabled && <button onClick={() => void sendDailyDigest()}>立即发送今日日报</button>}<button onClick={() => void act(async () => { setDigest(await api.refreshSocialDigest(activeAuthor.id)) })}>重新总结</button></div></div>
            {digest ? <><p className="digest-summary">{digest.summary}</p><div className="digest-highlights">{digest.highlights.map((item, index) => <article className={`importance-${item.importance}`} key={`${item.text}-${index}`}><span>{activityLabels[item.category] ?? item.category} · {item.factuality === 'fact' ? '明确事实' : item.factuality === 'plan' ? '作者计划' : '推测'}</span><p>{item.text}</p><small>证据 {item.post_ids.map((id, evidenceIndex) => { const post = rawPosts.find((candidate) => candidate.id === id); return post ? <span key={id}>{evidenceIndex > 0 ? '、' : ''}<a href={post.url} target="_blank" rel="noreferrer">帖子 {id} ↗</a></span> : <span key={id}>{evidenceIndex > 0 ? '、' : ''}帖子 {id}</span> })}</small></article>)}</div>{digest.uncertainties.length > 0 && <div className="digest-uncertain"><strong>仍不确定</strong><ul>{digest.uncertainties.map((item) => <li key={item}>{item}</li>)}</ul></div>}<small className="digest-source">{digest.generated_by === 'agent' ? `Agent 总结 · ${digest.model ?? ''}` : '规则降级摘要'}{digest.error ? ` · Agent 暂不可用：${digest.error}` : ''}</small></> : <p className="review-empty">扫描到作者动态后，这里会生成最近 7 天摘要。</p>}
          </section>
          <section className="activity-list">
            {activities.map((item) => { const post = item.posts.at(-1); const image = post?.media.find((media) => media.type === 'image')?.url; const availability = post ? availabilityLabel(post) : null; return <article className={`${item.is_read ? '' : 'unread'} importance-${item.importance}`} key={item.id}><div className="activity-thumb">{image ? <img src={image} alt="" /> : <Article size={30} weight="thin" />}</div><div><div className="activity-meta"><b>{activityLabels[item.category] ?? item.category}</b><span>{item.author_name}</span><time>{new Date(item.ended_at).toLocaleString()}</time></div><h3>{item.headline}</h3><p>{item.summary}</p>{availability && <small className="post-availability">{availability}</small>}<div className="activity-actions"><span>{item.importance === 'critical' ? '紧急' : item.importance === 'high' ? '重要' : item.importance === 'normal' ? '一般' : '低优先级'} · 置信度 {Math.round(item.confidence * 100)}%</span>{post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => { void api.markActivityRead(item.id); verifyPost(post) }}>查看原帖 ↗</a>}</div></div></article> })}
            {!activities.length && <div className="review-empty">还没有可展示的作者近况。</div>}
          </section>
        </>}

        {view === 'releases' && <><nav className="radar-filters" aria-label="作品情报筛选">
          {[['all', '全部'], ['pending', '待确认'], ['confirmed', '已确认'], ['new_release', '新作'], ['event_participation', '参展'], ['rejected', '已排除']].map(([value, label]) => <button className={filter === value ? 'active' : ''} onClick={() => setFilter(value)} key={value}>{label}</button>)}
        </nav><section className="signal-list">
          {visible.map((signal) => {
            const post = signal.posts.at(-1)
            const image = post?.media.find((item) => item.type === 'image' && item.url)?.url
            return <article className={`${signal.is_read ? '' : 'unread'} ${focusSignalId === signal.id ? 'focused' : ''}`} id={`signal-${signal.id}`} key={signal.id}>
              <div className="signal-media">{image ? <img src={image} alt="帖子媒体" /> : <NewspaperClipping size={36} weight="thin" />}</div>
              <div className="signal-copy">
                <div className="signal-meta"><span className={`signal-kind kind-${signal.kind}`}>{kindLabels[signal.kind] ?? signal.kind}</span><b>{signal.author_name}</b><time>{new Date(post?.posted_at ?? signal.created_at).toLocaleString()}</time></div>
                <h3>{signal.title ?? '标题尚未确认'}</h3>
                <p className="signal-post-text">{post?.text || post?.ocr_text || '帖子没有可读取的文字'}</p>{post && availabilityLabel(post) && <small className="post-availability">{availabilityLabel(post)}</small>}
                <div className="signal-facts"><span>置信度 {Math.round(signal.confidence * 100)}%</span>{signal.event_code && <span>{signal.event_code}</span>}{signal.booth && <span>摊位 {signal.booth}</span>}<span>状态 {signal.status}</span></div>
                <div className="signal-evidence"><strong>支持证据</strong>{signal.evidence.length ? <ul>{signal.evidence.map((item) => <li key={item}>{item}</li>)}</ul> : <p>没有足够证据</p>}{signal.counter_evidence.length > 0 && <><strong className="counter">反证 / 风险</strong><ul>{signal.counter_evidence.map((item) => <li key={item}>{item}</li>)}</ul></>}</div>
                <div className="signal-actions">
                  {post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => { void api.markSocialSignalRead(signal.id); verifyPost(post) }}>查看原帖 ↗</a>}
                  {signal.linked_group_id ? <button onClick={() => onOpenWork(signal.linked_group_id!)}>打开关联作品</button> : <><select value={links[signal.id] ?? ''} onChange={(event) => setLinks({ ...links, [signal.id]: Number(event.target.value) })}><option value="">关联现有作品…</option>{works.map((work) => <option value={work.id} key={work.id}>{work.title}</option>)}</select><button disabled={!links[signal.id]} onClick={() => void act(() => api.linkSocialSignal(signal.id, links[signal.id]))}>关联</button></>}
                  {signal.status === 'pending' && <><button className="reject" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'reject'))}>不是新情报</button><button className="confirm" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'confirm'))}>确认情报</button></>}
                </div>
              </div>
            </article>
          })}
          {!visible.length && <div className="review-empty">当前筛选下没有作者动态。</div>}
        </section></>}

        {view === 'raw' && <section className="raw-posts"><div className="raw-post-filter"><strong>原始事实流</strong><select value={postType} onChange={(event) => { const value = event.target.value; setPostType(value); void loadActivityData(value) }}><option value="">全部类型</option><option value="original">原创</option><option value="quote">引用</option><option value="reply">回复</option><option value="retweet">转推</option></select></div>{rawPosts.map((post) => <article key={post.id}><div><b>{post.post_type === 'original' ? '原创' : post.post_type === 'quote' ? '引用' : post.post_type === 'reply' ? '回复' : '转推'}</b><time>{new Date(post.posted_at).toLocaleString()}</time></div><p>{post.text || post.ocr_text || '无文字内容'}</p>{post.media.length > 0 && <div className="raw-media">{post.media.filter((item) => item.type === 'image' && item.url).slice(0, 4).map((item) => <img src={item.url} alt="" key={item.url} />)}</div>}{availabilityLabel(post) && <small className="post-availability">{availabilityLabel(post)}</small>}<a href={post.url} target="_blank" rel="noreferrer" onClick={() => verifyPost(post)}>打开原帖 ↗</a></article>)}{!rawPosts.length && <div className="review-empty">还没有采集到原始动态。</div>}</section>}
            </> : <div className="radar-no-authors"><UserCircle size={58} weight="thin" /><h3>还没有订阅作者</h3><p>添加作者后，就可以在这里直接切换并查看动态。</p></div>}
          </div>
        </div>
      </section>
    </div>
  )
}
